"""Production API services wired into the desktop task runner.

The desktop worker creates a fresh OpenAPI client for every serialized task.
That keeps ``asyncio`` ownership simple, reloads edited encrypted credentials,
and guarantees the HTTP pool is closed before the next task begins.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from erp_automation.configuration import EncryptedConfigurationStore
from erp_automation.integrations.lingxing import LingxingOpenAPIClient
from erp_automation.integrations.lingxing.runtime import create_lingxing_openapi_client
from erp_automation.persistence import CustomWorkflowStore
from erp_automation.ui.models import (
    Capability as UiCapability,
    CapabilityMode as UiCapabilityMode,
    CapabilityPolicy,
    DesktopSettings,
)
from shipment_automation.config import SHIPMENT_TAG_NAME
from shipment_automation.queue_store import ShipmentQueueStore

from .api_scanners import (
    ApiScanState,
    CustomizationApiScanResult,
    ShipmentApiScanResult,
    scan_customization_candidates,
    scan_shipment_candidates,
)
from .capabilities import (
    Capability as ApiCapability,
    CapabilityMode as ApiCapabilityMode,
    CapabilityRouter,
)
from .lingxing_gateway import LingxingGateway
from .custom_order_api import LingxingCustomOrderApiOperations


ClientFactory = Callable[[DesktopSettings], Awaitable[LingxingOpenAPIClient]]
PolicyProvider = Callable[[], CapabilityPolicy]


_UI_TO_API_CAPABILITY: dict[UiCapability, tuple[ApiCapability, ...]] = {
    UiCapability.LIST_ORDERS: (ApiCapability.LIST_ORDERS,),
    UiCapability.GET_ORDER_DETAIL: (ApiCapability.GET_ORDER_DETAIL,),
    UiCapability.UPDATE_CONTACT: (
        ApiCapability.UPDATE_PHONE,
        ApiCapability.UPDATE_BUYER_EMAIL,
    ),
    UiCapability.UPDATE_REMARK: (ApiCapability.UPDATE_REMARK,),
    UiCapability.DOWNLOAD_CUSTOM_ZIP: (ApiCapability.DOWNLOAD_ATTACHMENT,),
    UiCapability.EDIT_ORDER_ITEMS: (ApiCapability.UPDATE_ORDER_ITEMS,),
    UiCapability.SPLIT_ORDER: (ApiCapability.SPLIT_ORDER,),
    UiCapability.SET_LOGISTICS_CHANNEL: (ApiCapability.SET_SHIPPING_CHANNEL,),
    UiCapability.AUDIT_ORDER: (ApiCapability.REVIEW_ORDER,),
    UiCapability.UPDATE_TRACKING: (ApiCapability.UPDATE_TRACKING,),
    UiCapability.OUTBOUND_ORDER: (ApiCapability.OUTBOUND_ORDER,),
    UiCapability.ALIBABA_LOGISTICS: (ApiCapability.ALIBABA_LOGISTICS,),
    UiCapability.EMAIL_PREVIEW: (ApiCapability.SEND_EMAIL,),
}


def build_capability_router(
    policy: CapabilityPolicy,
    *,
    writes_enabled_provider: Callable[[], bool] | None = None,
) -> CapabilityRouter:
    """Translate the visible desktop policy to the API integration policy."""

    modes: dict[ApiCapability, ApiCapabilityMode] = {}
    for ui_capability, api_capabilities in _UI_TO_API_CAPABILITY.items():
        ui_mode = policy.effective_mode_for(ui_capability)
        if ui_mode is UiCapabilityMode.DISABLED:
            api_mode = ApiCapabilityMode.DISABLED
        elif ui_mode is UiCapabilityMode.BROWSER:
            api_mode = ApiCapabilityMode.BROWSER_ONLY
        else:
            api_mode = ApiCapabilityMode.API_PREFERRED
        for api_capability in api_capabilities:
            modes[api_capability] = api_mode

    # These capabilities have no official OpenAPI implementation.  They stay
    # browser-only even though the UI groups phone and email as one contact
    # operation, and real email sending stays disabled by product policy.
    modes[ApiCapability.UPDATE_BUYER_EMAIL] = ApiCapabilityMode.BROWSER_ONLY
    modes[ApiCapability.READ_FULL_ADDRESS] = ApiCapabilityMode.BROWSER_ONLY
    modes[ApiCapability.ALIBABA_LOGISTICS] = ApiCapabilityMode.BROWSER_ONLY
    modes[ApiCapability.SEND_EMAIL] = ApiCapabilityMode.DISABLED
    return CapabilityRouter(
        modes,
        writes_enabled=(
            writes_enabled_provider
            if writes_enabled_provider is not None
            else not policy.emergency_stop_writes
        ),
    )


class DesktopApiServices:
    """API-first scans and gateway creation for one desktop workspace."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        configuration_store: EncryptedConfigurationStore,
        policy_provider: PolicyProvider,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.configuration_store = configuration_store
        self.policy_provider = policy_provider
        self._client_factory = client_factory

    async def create_gateway(
        self,
        settings: DesktopSettings,
    ) -> tuple[LingxingGateway, LingxingOpenAPIClient]:
        if self._client_factory is not None:
            client = await self._client_factory(settings)
        else:
            client = await create_lingxing_openapi_client(
                self.configuration_store,
                base_url=settings.lingxing_api_base_url,
                timeout=float(settings.api_timeout_seconds),
            )
        policy = self.policy_provider()
        router = build_capability_router(
            policy,
            writes_enabled_provider=lambda: not self.policy_provider().emergency_stop_writes,
        )
        return LingxingGateway(client, router), client

    @asynccontextmanager
    async def custom_order_operations(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
    ) -> AsyncIterator[LingxingCustomOrderApiOperations]:
        """Create and close one custom-order API adapter per desktop task.

        ``DesktopTaskRunner`` executes each command with a fresh
        ``asyncio.run`` loop.  Creating the OpenAPI client here keeps its HTTP
        pool bound to that same loop and the ``finally`` block guarantees that
        no client leaks into a later command.
        """

        del configuration  # credentials are read directly from config.enc
        gateway, client = await self.create_gateway(settings)
        try:
            yield LingxingCustomOrderApiOperations(gateway)
        finally:
            await client.aclose()

    async def scan_custom_orders(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del configuration  # credentials are read directly from config.enc
        store = CustomWorkflowStore(self._path(settings.custom_state_path))
        gateway, client = await self.create_gateway(settings)
        try:
            result = await scan_customization_candidates(
                gateway,
                store,
                filters=self._pending_payment_filters(settings.payment_window_hours),
            )
        finally:
            await client.aclose()

        self._persist_custom_candidates(store, result)
        rows = [
            {
                "platform_order_no": candidate.platform_order_no,
                "system_order_no": candidate.system_order_no,
                "product_type": candidate.product_type or "",
                "workflow_stage": "candidate",
                "status_text": "待处理",
                "last_error": "",
            }
            for candidate in result.candidates
        ]
        return {
            "status": self._task_status(result.state),
            "message": self._custom_scan_message(result),
            "custom_orders": rows,
            "candidate_count": result.candidate_count,
            "row_count": result.row_count,
            "payment_window_hours": int(result.payment_window_hours),
            "request_ids": list(result.pagination.request_ids),
            "diagnostic_codes": [item.code for item in result.diagnostics],
        }

    async def test_connection(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Issue one harmless documented read to validate auth/signing/token state."""

        del configuration
        gateway, client = await self.create_gateway(settings)
        try:
            page = await gateway.list_orders(
                offset=0,
                # The live endpoint rejects lengths below 20 even though the
                # documentation only states the upper bound explicitly.
                length=20,
                filters=self._pending_payment_filters(settings.payment_window_hours),
            )
        finally:
            await client.aclose()
        return {
            "status": "completed",
            "message": "领星 OpenAPI 连接成功，Token 和请求签名均已验证。",
            "request_ids": [page.request_id] if page.request_id else [],
        }

    async def scan_shipments(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del configuration
        queue = ShipmentQueueStore(self._path(settings.queue_path))
        gateway, client = await self.create_gateway(settings)
        try:
            result = await scan_shipment_candidates(
                gateway,
                queue,
                SHIPMENT_TAG_NAME,
                filters=self._pending_payment_filters(settings.payment_window_hours),
                dry_run=False,
                # This is a documented 96-hour filtered slice, not the whole
                # lifetime pending-order universe.  Absence from this slice
                # can never prove that an older queued order was completed.
                reconcile_missing=False,
            )
        finally:
            await client.aclose()
        return {
            "status": self._task_status(result.state),
            "message": self._shipment_scan_message(result),
            "candidate_count": result.candidate_count,
            "enqueued_count": result.enqueued_count,
            "manual_completed_count": result.manual_completed_count,
            "row_count": result.row_count,
            "request_ids": list(result.pagination.request_ids),
            "diagnostic_codes": [item.code for item in result.diagnostics],
        }

    def _pending_payment_filters(self, hours: int) -> dict[str, Any]:
        """Return documented, double-open query bounds plus business filtering."""

        now = datetime.now(timezone.utc)
        # The API uses an open interval.  The extra minute avoids losing orders
        # exactly on a boundary; customization candidate logic reapplies the
        # exact confirmed 96-hour rule after normalization.
        start = now - timedelta(hours=max(1, int(hours)), minutes=1)
        end = now + timedelta(minutes=1)
        return {
            "date_type": "global_payment_time",
            "start_time": int(start.timestamp()),
            "end_time": int(end.timestamp()),
            "platform_code": [10001],
            "order_status": 4,
            "include_delete": False,
        }

    @staticmethod
    def _persist_custom_candidates(
        store: CustomWorkflowStore,
        result: CustomizationApiScanResult,
    ) -> None:
        seen_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        for candidate in result.candidates:
            # Never rewrite an existing workflow during a scan: doing so could
            # erase an operator's stage state or error annotation.
            if store.get_workflow(candidate.platform_order_no) is not None:
                continue

            def initial_record(_old: dict[str, Any], *, item=candidate) -> dict[str, Any]:
                return {
                    "platform_order_no": item.platform_order_no,
                    "system_order_no": item.system_order_no,
                    "product_type": item.product_type,
                    "workflow_status": "pending",
                    "last_seen_at": seen_at,
                }

            store.mutate_legacy_record(
                candidate.platform_order_no,
                initial_record,
                event_type="api_candidate_seen",
                actor="api_scanner",
            )

    def _path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.workspace / path

    @staticmethod
    def _task_status(state: ApiScanState) -> str:
        return {
            ApiScanState.COMPLETE: "completed",
            ApiScanState.INCOMPLETE: "incomplete",
            ApiScanState.FAILED: "failed",
        }[state]

    @staticmethod
    def _custom_scan_message(result: CustomizationApiScanResult) -> str:
        if result.state is ApiScanState.COMPLETE:
            return f"API 扫描完成：发现 {result.candidate_count} 个 96 小时内的待处理定制订单。"
        return "API 订单快照不完整，已保留看到的候选项，禁止作为完整结果继续自动化。"

    @staticmethod
    def _shipment_scan_message(result: ShipmentApiScanResult) -> str:
        if result.state is ApiScanState.COMPLETE:
            return f"API 扫描完成：新增 {result.enqueued_count} 个自动标发任务。"
        return "API 待审核快照不完整，已停止缺失订单结案判定。"


__all__ = ["DesktopApiServices", "build_capability_router"]
