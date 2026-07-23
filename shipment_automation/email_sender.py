from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import DEFAULT_SHIPMENT_QUEUE_PATH
from .models import EmailBatchPreview
from .queue_store import ShipmentWorkflowStore


def build_customer_email_preview(batch: EmailBatchPreview) -> dict[str, Any]:
    """Build a local-only preview. This module never connects to an email provider."""

    payload = asdict(batch)
    draft_ready = bool((batch.recipient_email or "").strip())
    payload.update(
        {
            "send_enabled": False,
            "delivery_mode": "preview_only",
            "delivery_status": "draft_ready" if draft_ready else "draft_needs_recipient",
            "send_attempted": False,
            "message": (
                "真实邮件发送暂未启用；已生成本地草稿，未向外部邮箱发送。"
                if draft_ready
                else "真实邮件发送暂未启用；已生成本地草稿，但仍需补充收件邮箱。"
            ),
            "subject": f"Shipment update for order {batch.platform_order_no}",
            "body_lines": [
                f"{logistics_no}: {tracking_no or '-'}"
                for logistics_no, tracking_no in zip(batch.logistics_numbers, batch.tracking_numbers)
            ],
        }
    )
    return payload


def list_customer_email_previews(
    queue_path: str | Path = DEFAULT_SHIPMENT_QUEUE_PATH,
) -> list[dict[str, Any]]:
    """List historical previews without creating any new mail records."""

    store = ShipmentWorkflowStore(queue_path)
    return [build_customer_email_preview(batch) for batch in store.list_email_batches()]


def send_customer_email(batch: EmailBatchPreview, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """安全兼容旧发送入口，始终返回本地草稿且绝不连接邮件服务。

    保留这个函数名是为了让既有工作流无需因暂停真实发信而中断。即使调用方
    请求“发送”，当前实现也只会生成预览，并明确报告没有发生外部发送。
    """

    preview = build_customer_email_preview(batch)
    preview.update(
        {
            "delivery_status": "not_sent_preview_only",
            "message": "真实邮件发送暂未启用；发送请求已转换为本地草稿，流程可继续。",
        }
    )
    return preview
