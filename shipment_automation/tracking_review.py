from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .models import TRACKING_REVIEW_AUTO_RECHECK, TRACKING_REVIEW_ORDER_ISSUE
from .queue_store import ShipmentWorkflowStore


InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]


@dataclass
class TrackingReviewSummary:
    seen_logistics_numbers: set[str] = field(default_factory=set)
    auto_recheck_count: int = 0
    order_issue_count: int = 0
    confirmed_count: int = 0
    deferred_count: int = 0

    @property
    def reviewed_count(self) -> int:
        return self.auto_recheck_count + self.order_issue_count + self.confirmed_count

    def merge(self, other: "TrackingReviewSummary") -> None:
        self.seen_logistics_numbers.update(other.seen_logistics_numbers)
        self.auto_recheck_count += other.auto_recheck_count
        self.order_issue_count += other.order_issue_count
        self.confirmed_count += other.confirmed_count
        self.deferred_count += other.deferred_count


def review_pending_tracking_mismatches(
    store: ShipmentWorkflowStore,
    *,
    input_func: InputFunc | None = None,
    output_func: OutputFunc | None = None,
    exclude_logistics_numbers: set[str] | None = None,
) -> TrackingReviewSummary:
    input_func = input_func or input
    output_func = output_func or print
    excluded = exclude_logistics_numbers or set()
    summary = TrackingReviewSummary()
    for item in store.list_pending_tracking_mismatch_reviews():
        logistics_no = str(item.get("logistics_no") or "")
        if not logistics_no or logistics_no in excluded:
            continue
        summary.seen_logistics_numbers.add(logistics_no)
        output_func("\n发现承运商与国际物流单号不匹配，请审核：")
        output_func(f"系统单号：{item.get('system_order_no') or '-'}")
        output_func(f"平台单号：{item.get('platform_order_no') or '-'}")
        output_func(f"物流单号：{logistics_no}")
        output_func(f"承运商：{item.get('carrier') or '-'}")
        output_func(f"国际物流单号：{item.get('international_tracking_no') or '-'}")
        output_func("1. 中间商单号，以后每三小时自动复查直到出现真实尾程单号")
        output_func("2. 订单有问题，永久阻止并停止自动查询")
        output_func("3. 确认当前单号，直接允许进入 ERP")
        answer = _read(input_func, "请输入 1、2 或 3，其他输入暂不处理并继续下一单：")
        if answer == "1":
            changed = store.set_tracking_mismatch_review(
                logistics_no,
                TRACKING_REVIEW_AUTO_RECHECK,
            )
            summary.auto_recheck_count += int(changed)
        elif answer == "2":
            changed = store.set_tracking_mismatch_review(
                logistics_no,
                TRACKING_REVIEW_ORDER_ISSUE,
            )
            summary.order_issue_count += int(changed)
        elif answer == "3":
            changed = store.confirm_tracking_override(
                logistics_no,
                reason="用户在自动巡检中确认当前尾程单号",
            )
            summary.confirmed_count += int(changed)
        else:
            summary.deferred_count += 1
            output_func("本轮暂不处理该订单，审核状态未修改。")
    return summary


def _read(input_func: InputFunc, prompt: str) -> str | None:
    try:
        return str(input_func(prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        return None
