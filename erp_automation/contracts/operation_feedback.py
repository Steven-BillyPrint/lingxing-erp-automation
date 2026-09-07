"""Consistent, concise feedback for refused user operations and batch items."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .controller import ControlResult


@dataclass(frozen=True)
class OperationRejection:
    title: str
    message: str


def _short_reason(value: object, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "服务器未提供具体原因，请查看日志。"
    if "后台仍有等待或运行中的任务" in text:
        return "后台仍有任务在等待或运行，请在任务结束后重试。"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def operation_rejection(result: ControlResult) -> OperationRejection | None:
    """A legacy non_modal hint must never hide a definitive rejection."""

    details = result.details
    if details.get("rejection_notice_shown"):
        return None
    rejected: dict[str, str] = {}
    skipped = details.get("skipped_reasons")
    if isinstance(skipped, Mapping):
        rejected.update((str(key), str(value)) for key, value in skipped.items())
    for key, reason in details.get("rejected_orders", ()):
        rejected[str(key)] = str(reason)
    for receipt in details.get("submission_receipts", ()):
        item = receipt.result
        if not item.accepted and not item.details.get("submission_outcome_unknown"):
            rejected[receipt.order_no or receipt.logistics_no] = item.message
    if rejected:
        lines = [
            f"{key}：{_short_reason(reason, 120)}"
            for key, reason in list(rejected.items())[:3]
        ]
        if len(rejected) > 3:
            lines.append(f"另有 {len(rejected) - 3} 条，完整原因见日志。")
        return OperationRejection(
            "部分操作被拒绝" if result.accepted else "操作被拒绝",
            f"{len(rejected)} 条操作未执行：\n" + "\n".join(lines),
        )
    if result.accepted or details.get("automatic_retry"):
        return None
    if details.get("submission_outcome_unknown"):
        return None
    return OperationRejection(
        "操作未完成" if details.get("server_execution_failed") or details.get("operation_failed") else "操作被拒绝",
        _short_reason(result.message),
    )
