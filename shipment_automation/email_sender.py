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
    payload.update(
        {
            "send_enabled": False,
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
    store = ShipmentWorkflowStore(queue_path)
    store.prepare_email_batches()
    return [build_customer_email_preview(batch) for batch in store.list_email_batches()]


def send_customer_email(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("真实邮件发送尚未启用；当前版本只生成本地邮件批次和预览。")
