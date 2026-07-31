from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any

from .notification_domain import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    NOTIFICATION_ACCEPTED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_FAILED,
    NOTIFICATION_RETRYABLE,
    NotificationConfiguration,
)
from .notification_providers import (
    AlimailClient,
    ClickSendClient,
    NotificationProviderError,
)
from .notification_store import ShipmentNotificationStore


DELIVERY_POLL_TIMEOUT_SECONDS = 60.0
DELIVERY_POLL_INTERVAL_SECONDS = 5.0
_STATUS_CHECK_FAILURE_PREFIXES = ("状态核验超时：", "状态查询失败：")


def _is_status_check_failure(notification: dict[str, Any]) -> bool:
    message = str(notification.get("last_error") or "")
    return message.startswith(_STATUS_CHECK_FAILURE_PREFIXES)


class ShipmentNotificationService:
    """The only component allowed to turn an approved draft into network I/O."""

    def __init__(
        self,
        store: ShipmentNotificationStore,
        configuration: NotificationConfiguration,
        *,
        timeout_seconds: float = 30,
        delivery_poll_timeout_seconds: float = DELIVERY_POLL_TIMEOUT_SECONDS,
        delivery_poll_interval_seconds: float = DELIVERY_POLL_INTERVAL_SECONDS,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        alimail_client: AlimailClient | None = None,
        clicksend_client: ClickSendClient | None = None,
    ) -> None:
        self.store = store
        self.configuration = configuration
        self.timeout_seconds = timeout_seconds
        self.delivery_poll_timeout_seconds = max(0.0, float(delivery_poll_timeout_seconds))
        self.delivery_poll_interval_seconds = max(
            0.1, float(delivery_poll_interval_seconds)
        )
        self._sleeper = sleeper
        self._alimail = alimail_client
        self._clicksend = clicksend_client
        self._owns_alimail = alimail_client is None
        self._owns_clicksend = clicksend_client is None

    def _alimail_client(self) -> AlimailClient:
        if self._alimail is None:
            self._alimail = AlimailClient(
                self.configuration.alimail_app_id,
                self.configuration.alimail_app_secret,
                timeout_seconds=self.timeout_seconds,
            )
        return self._alimail

    def _clicksend_client(self) -> ClickSendClient:
        if self._clicksend is None:
            self._clicksend = ClickSendClient(
                self.configuration.clicksend_username,
                self.configuration.clicksend_api_key,
                sender_id=self.configuration.clicksend_sender_id,
                timeout_seconds=self.timeout_seconds,
            )
        return self._clicksend

    async def aclose(self) -> None:
        if self._owns_alimail and self._alimail is not None:
            await self._alimail.aclose()
        if self._owns_clicksend and self._clicksend is not None:
            await self._clicksend.aclose()

    async def approve_and_send(
        self,
        notification_id: int,
        *,
        actor: str = "desktop_user",
    ) -> dict[str, Any]:
        notification = self.store.approve_and_claim(
            notification_id, self.configuration, actor=actor
        )
        return await self._send_claimed(notification)

    async def retry_approved_content(
        self,
        notification_id: int,
        *,
        actor: str = "desktop_user",
    ) -> dict[str, Any]:
        notification = self.store.retry_approved_and_claim(
            notification_id, self.configuration, actor=actor
        )
        return await self._send_claimed(notification)

    async def _send_claimed(self, notification: dict[str, Any]) -> dict[str, Any]:
        notification_id = int(notification["id"])
        try:
            if notification["channel"] == CHANNEL_EMAIL:
                acceptance = await self._alimail_client().send(
                    sender_email=str(notification["sender_email"]),
                    sender_name=self.configuration.sender_display_name,
                    recipient_email=str(notification["target"]),
                    recipient_name=str(notification["recipient_name"]),
                    subject=str(notification["subject"]),
                    body=str(notification["body"]),
                    idempotency_key=str(notification["idempotency_key"]),
                    body_html=str(notification.get("body_html") or ""),
                )
            elif notification["channel"] == CHANNEL_SMS:
                acceptance = await self._clicksend_client().send(
                    to=str(notification["target"]),
                    body=str(notification["body"]),
                    idempotency_key=str(notification["idempotency_key"]),
                )
            else:
                raise NotificationProviderError("Notification channel is not configured.")
        except NotificationProviderError as exc:
            self.store.finalize_send(
                notification_id,
                accepted=False,
                retryable=exc.retryable,
                # Provider exceptions contain only sanitized, authored messages.
                error=f"发送请求失败：{exc}",
            )
            raise
        except Exception as exc:
            self.store.finalize_send(
                notification_id,
                accepted=False,
                retryable=True,
                error=f"发送请求失败：供应商请求发生意外错误（{type(exc).__name__}）。",
            )
            raise NotificationProviderError(
                "The provider request failed unexpectedly.", retryable=True
            ) from exc
        self.store.finalize_send(
            notification_id,
            accepted=True,
            provider_message_id=acceptance.message_id,
            provider_status=acceptance.status,
        )
        result = self.store.get_notification(notification_id)
        if result is None:
            raise RuntimeError("Accepted notification disappeared from local storage.")
        return result

    async def approve_send_and_wait(
        self,
        notification_id: int,
        *,
        actor: str = "desktop_user",
    ) -> dict[str, Any]:
        await self.approve_and_send(notification_id, actor=actor)
        return await self.wait_for_delivery(notification_id)

    async def retry_send_and_wait(
        self,
        notification_id: int,
        *,
        actor: str = "desktop_user",
    ) -> dict[str, Any]:
        await self.retry_approved_content(notification_id, actor=actor)
        return await self.wait_for_delivery(notification_id)

    async def refresh_clicksend_receipt(self, notification_id: int) -> dict[str, Any]:
        return await self.refresh_delivery_receipt(notification_id)

    async def refresh_delivery_receipt(self, notification_id: int) -> dict[str, Any]:
        notification = self.store.get_notification(notification_id)
        if notification is None:
            raise ValueError("Notification does not exist.")
        state = str(notification.get("state") or "")
        allowed = state in {NOTIFICATION_ACCEPTED, NOTIFICATION_DELIVERED} or (
            state == NOTIFICATION_FAILED and _is_status_check_failure(notification)
        )
        if not allowed:
            raise ValueError("Notification has not been accepted by its provider.")
        message_id = str(notification.get("provider_message_id") or "")
        if not message_id:
            raise ValueError("Provider message id is missing.")
        if notification["channel"] == CHANNEL_EMAIL:
            receipt = await self._alimail_client().receipt(
                sender_email=str(notification.get("sender_email") or ""),
                message_id=message_id,
                idempotency_key=str(notification.get("idempotency_key") or ""),
                subject=str(notification.get("subject") or ""),
                recipient_email=str(notification.get("target") or ""),
                sent_at=str(notification.get("sent_at") or ""),
            )
            send_status = str(receipt.get("send_status") or "").strip().lower()
            resolved_message_id = str(receipt.get("message_id") or "").strip()
            if resolved_message_id and resolved_message_id != message_id:
                self.store.update_provider_message_id(
                    notification_id,
                    provider_message_id=resolved_message_id,
                )
            if send_status == "success":
                self.store.mark_delivered(
                    notification_id,
                    provider_status=send_status,
                )
            elif send_status == "failed":
                self.store.mark_delivery_failed(
                    notification_id,
                    provider_status=send_status,
                    error=(
                        "发送失败：阿里邮箱明确返回发送失败。"
                        "请核对收件地址和供应商详情后重试已批准内容。"
                    ),
                )
            else:
                self.store.update_provider_status(
                    notification_id,
                    provider_status=send_status,
                )
        elif notification["channel"] == CHANNEL_SMS:
            receipt = await self._clicksend_client().history(
                message_id,
                sent_at=str(notification.get("sent_at") or ""),
            )
            status = str(receipt.get("status") or "").strip()
            status_lower = status.lower()
            status_code = str(receipt.get("status_code") or "").strip()
            status_text = str(receipt.get("status_text") or "").strip()
            provider_status = " / ".join(
                part for part in (status, status_code, status_text) if part
            ) or "ClickSend 历史暂未出现该消息"
            if status_code == "201":
                self.store.mark_delivered(
                    notification_id,
                    provider_status=provider_status,
                )
            elif status_code == "301" or status_lower in {
                "failed",
                "cancelled",
                "cancelledafterreview",
            }:
                self.store.mark_delivery_failed(
                    notification_id,
                    provider_status=provider_status,
                    error=(
                        f"发送失败：ClickSend 明确返回“{provider_status}”。"
                        "请核对电话号码和供应商详情后重试已批准内容。"
                    ),
                )
            else:
                self.store.update_provider_status(
                    notification_id,
                    provider_status=provider_status,
                )
        else:
            raise ValueError("Notification channel does not support delivery receipts.")
        refreshed = self.store.get_notification(notification_id)
        if refreshed is None:
            raise RuntimeError("Notification disappeared from local storage.")
        return refreshed

    async def wait_for_delivery(self, notification_id: int) -> dict[str, Any]:
        """Poll an accepted notification until delivery, failure or timeout."""

        timeout = self.delivery_poll_timeout_seconds
        interval = self.delivery_poll_interval_seconds
        attempts = max(1, int(math.ceil(timeout / interval)) + 1)
        successful_checks = 0
        query_errors = 0
        last_query_error = ""
        for attempt in range(attempts):
            try:
                refreshed = await self.refresh_delivery_receipt(notification_id)
                successful_checks += 1
                if refreshed["state"] in {
                    NOTIFICATION_DELIVERED,
                    NOTIFICATION_RETRYABLE,
                }:
                    return refreshed
            except (NotificationProviderError, ValueError) as exc:
                query_errors += 1
                last_query_error = type(exc).__name__
            if attempt + 1 < attempts:
                await self._sleeper(interval)

        current = self.store.get_notification(notification_id)
        if current is None:
            raise RuntimeError("Notification disappeared from local storage.")
        provider_status = str(current.get("provider_status") or "").strip()
        if successful_checks:
            reason = (
                f"状态核验超时：发送服务已接收通知，但在 {int(timeout)} 秒内"
                "没有返回已送达或明确失败状态。"
                f"最后状态：{provider_status or '无更新'}。"
                "这不等于发送失败，请先刷新发送状态，避免重复发送。"
            )
        else:
            reason = (
                f"状态查询失败：发送服务已接收通知，但连续 {query_errors} 次"
                f"状态查询均失败（{last_query_error or '未知查询错误'}）。"
                "这不等于发送失败，请先刷新发送状态，避免重复发送。"
            )
        self.store.mark_delivery_status_check_failed(
            notification_id,
            provider_status=provider_status or "状态尚未确认",
            error=reason,
        )
        failed = self.store.get_notification(notification_id)
        if failed is None:
            raise RuntimeError("Notification disappeared from local storage.")
        return failed

    async def refresh_pending_receipts(self) -> dict[str, int]:
        notifications = self.store.list_notifications(
            states=(NOTIFICATION_ACCEPTED, NOTIFICATION_FAILED),
            latest_only=False,
        )
        notifications = [
            item
            for item in notifications
            if item.get("state") == NOTIFICATION_ACCEPTED
            or _is_status_check_failure(item)
        ]
        result = {
            "checked": 0,
            "completed": 0,
            "retryable": 0,
            "status_check_failed": 0,
            "errors": 0,
        }
        for notification in notifications:
            try:
                refreshed = await self.refresh_delivery_receipt(int(notification["id"]))
            except (NotificationProviderError, ValueError):
                result["errors"] += 1
                continue
            result["checked"] += 1
            if refreshed["state"] == NOTIFICATION_DELIVERED:
                result["completed"] += 1
            elif refreshed["state"] == "RETRYABLE":
                result["retryable"] += 1
            elif refreshed["state"] == NOTIFICATION_FAILED:
                result["status_check_failed"] += 1
        return result

    async def test_alimail_connection(self) -> bool:
        return await self._alimail_client().test_connection()

    async def test_clicksend_connection(self) -> bool:
        return await self._clicksend_client().test_connection()


__all__ = [
    "DELIVERY_POLL_INTERVAL_SECONDS",
    "DELIVERY_POLL_TIMEOUT_SECONDS",
    "ShipmentNotificationService",
]
