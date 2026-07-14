"""Production API services wired into the desktop task runner.

The desktop worker creates a fresh OpenAPI client for every serialized task.
That keeps ``asyncio`` ownership simple, reloads edited encrypted credentials,
and guarantees the HTTP pool is closed before the next task begins.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from erp_automation.configuration import EncryptedConfigurationStore
from erp_automation.integrations.lingxing import LingxingOpenAPIClient
from erp_automation.integrations.lingxing.runtime import create_lingxing_openapi_client
from erp_automation.operations.scan_audit import ScanAuditWriteResult, ScanAuditWriter
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


_SCAN_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_SHIPMENT_SCAN_DAYS = 30
_SHIPMENT_API_WINDOW_SECONDS = 30 * 24 * 60 * 60
_SHIPMENT_WINDOW_OVERLAP_SECONDS = 1


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
        task_id: str | None = None,
    ) -> Mapping[str, Any]:
        del configuration  # credentials are read directly from config.enc
        audit_task_id = self._scan_task_id(task_id, scan_kind="customization")
        started_at = datetime.now(timezone.utc)
        filters = self._custom_order_filters(settings.payment_window_hours)
        store: CustomWorkflowStore | None = None
        result: CustomizationApiScanResult | None = None
        try:
            store = CustomWorkflowStore(self._path(settings.custom_state_path))
            gateway, client = await self.create_gateway(settings)
            try:
                result = await scan_customization_candidates(
                    gateway,
                    store,
                    filters=filters,
                )
            finally:
                await client.aclose()
            if result.complete:
                self._persist_custom_candidates(store, result)
        except Exception as error:
            return self._failed_scan_payload(
                settings=settings,
                task_id=audit_task_id,
                scan_kind="customization",
                started_at=started_at,
                query=filters,
                error=error,
                pages=result.pagination.page_traces if result is not None else (),
                order_decisions=(
                    self._custom_audit_decisions(result) if result is not None else ()
                ),
                summary=self._custom_audit_summary(
                    result,
                    settings.payment_window_hours,
                    status="failed",
                ),
                payload_defaults={
                    "custom_orders": [],
                    "candidate_count": 0,
                    "row_count": 0,
                    "api_order_count": 0,
                    "processed_order_count": 0,
                    "skip_counts": {},
                    "payment_window_hours": int(settings.payment_window_hours),
                    "request_ids": [],
                    "diagnostic_codes": ["scan_runtime_failure"],
                },
            )

        rows = (
            [
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
            if result.complete
            else []
        )
        payload: dict[str, Any] = {
            "status": self._task_status(result.state),
            "message": self._custom_scan_message(result),
            "custom_orders": rows,
            "candidate_count": result.candidate_count,
            "row_count": result.row_count,
            "api_order_count": len(result.pagination.orders),
            "processed_order_count": result.processed_order_count,
            "skip_counts": dict(result.skip_counts),
            "payment_window_hours": int(result.payment_window_hours),
            "request_ids": list(result.pagination.request_ids),
            "diagnostic_codes": [item.code for item in result.diagnostics],
        }
        return self._complete_scan_payload(
            settings=settings,
            task_id=audit_task_id,
            scan_kind="customization",
            started_at=started_at,
            query=filters,
            pages=result.pagination.page_traces,
            order_decisions=self._custom_audit_decisions(result),
            summary=self._custom_audit_summary(
                result,
                settings.payment_window_hours,
                status=payload["status"],
            ),
            payload=payload,
        )

    async def test_connection(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        task_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Issue one harmless documented read to validate auth/signing/token state."""

        del configuration, task_id
        gateway, client = await self.create_gateway(settings)
        try:
            page = await gateway.list_orders(
                offset=0,
                # The live endpoint rejects lengths below 20 even though the
                # documentation only states the upper bound explicitly.
                length=20,
                filters=self._custom_order_filters(settings.payment_window_hours),
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
        task_id: str | None = None,
    ) -> Mapping[str, Any]:
        del configuration
        audit_task_id = self._scan_task_id(task_id, scan_kind="shipment")
        started_at = datetime.now(timezone.utc)
        filter_windows = self._shipment_order_filters()
        query = self._shipment_query_summary(filter_windows)
        queue: ShipmentQueueStore | None = None
        result: ShipmentApiScanResult | None = None
        queue_total_count: int | None = None
        email_preview_backfill_count = 0
        try:
            queue = ShipmentQueueStore(self._path(settings.queue_path))
            gateway, client = await self.create_gateway(settings)
            try:
                result = await scan_shipment_candidates(
                    gateway,
                    queue,
                    SHIPMENT_TAG_NAME,
                    filter_windows=filter_windows,
                    dry_run=False,
                    # This is the current pending-review table range, not the
                    # lifetime order universe.  Absence from it never proves
                    # that an older queued order was completed.
                    reconcile_missing=False,
                )
            finally:
                await client.aclose()
            if result.complete:
                # Local preview generation is intentionally independent from
                # the 30-day query range and from the customization payment
                # window.  The store keeps this operation idempotent and never
                # sends real email.
                email_preview_backfill_count = queue.prepare_email_batches_with_count()
            queue_total_count = len(queue.list_all_jobs())
        except Exception as error:
            if queue_total_count is None and queue is not None:
                try:
                    queue_total_count = len(queue.list_all_jobs())
                except Exception:
                    queue_total_count = None
            return self._failed_scan_payload(
                settings=settings,
                task_id=audit_task_id,
                scan_kind="shipment",
                started_at=started_at,
                query=query,
                error=error,
                pages=result.pagination.page_traces if result is not None else (),
                order_decisions=(
                    self._shipment_audit_decisions(result) if result is not None else ()
                ),
                summary=self._shipment_audit_summary(
                    result,
                    status="failed",
                    queue_total_count=queue_total_count,
                    query=query,
                    email_preview_backfill_count=email_preview_backfill_count,
                ),
                payload_defaults=self._shipment_payload_metrics(
                    result,
                    queue_total_count=queue_total_count,
                    query=query,
                    email_preview_backfill_count=email_preview_backfill_count,
                    extra_diagnostic_codes=("scan_runtime_failure",),
                ),
            )

        payload = self._shipment_payload_metrics(
            result,
            queue_total_count=queue_total_count,
            query=query,
            email_preview_backfill_count=email_preview_backfill_count,
        )
        payload.update({
            "status": self._task_status(result.state),
            "message": self._shipment_scan_message(
                result,
                queue_total_count,
                query=query,
                email_preview_backfill_count=email_preview_backfill_count,
            ),
        })
        return self._complete_scan_payload(
            settings=settings,
            task_id=audit_task_id,
            scan_kind="shipment",
            started_at=started_at,
            query=query,
            pages=result.pagination.page_traces,
            order_decisions=self._shipment_audit_decisions(result),
            summary=self._shipment_audit_summary(
                result,
                status=payload["status"],
                queue_total_count=queue_total_count,
                query=query,
                email_preview_backfill_count=email_preview_backfill_count,
            ),
            payload=payload,
        )

    @staticmethod
    def _scan_task_id(task_id: str | None, *, scan_kind: str) -> str:
        """Keep a valid desktop execution id or generate a path-safe one."""

        candidate = str(task_id or "").strip()
        if (
            candidate not in {".", ".."}
            and _SCAN_TASK_ID_RE.fullmatch(candidate) is not None
        ):
            return candidate
        return f"{scan_kind}-{uuid4().hex}"

    def _audit_writer(self, settings: DesktopSettings) -> ScanAuditWriter:
        # DesktopSettings validation fixes this value to ``logs``.  Refuse an
        # ad-hoc alternate root here as a second boundary, so audit retention
        # and path confinement cannot silently diverge from the visible app.
        configured = settings.log_dir.strip().replace("\\", "/").strip("/").casefold()
        if configured != "logs" or Path(settings.log_dir).is_absolute():
            raise ValueError("扫描审计日志目录必须是应用工作区下的 logs。")
        return ScanAuditWriter(self.workspace / "logs")

    def _complete_scan_payload(
        self,
        *,
        settings: DesktopSettings,
        task_id: str,
        scan_kind: str,
        started_at: datetime,
        query: Mapping[str, Any],
        pages: Any,
        order_decisions: Any,
        summary: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            audit = self._audit_writer(settings).write(
                task_id=task_id,
                scan_kind=scan_kind,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                query=query,
                pages=pages,
                order_decisions=order_decisions,
                summary=summary,
            )
        except Exception:
            output = dict(payload)
            output.update(
                {
                    "status": "failed",
                    "message": (
                        "API 扫描已经执行，但安全审计日志写入失败。"
                        f"任务 ID：{task_id}；请检查固定 logs 目录。"
                    ),
                    "task_id": task_id,
                    "audit_log_path": "",
                    "error_id": None,
                }
            )
            return output
        return self._attach_audit(payload, audit)

    def _failed_scan_payload(
        self,
        *,
        settings: DesktopSettings,
        task_id: str,
        scan_kind: str,
        started_at: datetime,
        query: Mapping[str, Any],
        error: Exception,
        pages: Any,
        order_decisions: Any,
        summary: Mapping[str, Any],
        payload_defaults: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            audit = self._audit_writer(settings).write(
                task_id=task_id,
                scan_kind=scan_kind,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                query=query,
                pages=pages,
                order_decisions=order_decisions,
                summary=summary,
                error=error,
            )
        except Exception:
            return {
                "status": "failed",
                "message": (
                    "API 扫描失败，且安全审计日志未能写入。"
                    f"任务 ID：{task_id}；原始错误信息已隐藏，请检查固定 logs 目录。"
                ),
                **dict(payload_defaults),
                "task_id": task_id,
                "audit_log_path": "",
                "error_id": None,
            }

        payload = {
            "status": "failed",
            "message": "API 扫描失败；原始错误信息已隐藏。",
            **dict(payload_defaults),
        }
        return self._attach_audit(payload, audit)

    @staticmethod
    def _attach_audit(
        payload: Mapping[str, Any],
        audit: ScanAuditWriteResult,
    ) -> Mapping[str, Any]:
        output = dict(payload)
        message = str(output.get("message") or "API 扫描已完成。")
        output.update(
            {
                "message": (
                    f"{message} 审计任务 ID：{audit.task_id}；"
                    f"日志：{audit.path}；错误编号：{audit.error_id or '无'}。"
                ),
                "task_id": audit.task_id,
                "audit_log_path": str(audit.path),
                "error_id": audit.error_id,
            }
        )
        return output

    @staticmethod
    def _custom_audit_decisions(result: CustomizationApiScanResult) -> tuple[Any, ...]:
        provided = getattr(result, "audit_decisions", None)
        if provided is not None:
            return tuple(provided)
        return tuple(
            {
                "platform_order_no": candidate.platform_order_no,
                "system_order_no": candidate.system_order_no,
                "source_page": candidate.source_page,
                "paid_at": candidate.paid_at_text,
                "decision": "candidate",
                "reason_code": "matched_supported_product",
                "matched_asins": candidate.matched_asins,
                "parent_asin": candidate.parent_asin,
                "product_type": candidate.product_type,
                "items": [{"asin": candidate.asin, "sku": candidate.sku}],
            }
            for candidate in result.candidates
        )

    @staticmethod
    def _shipment_audit_decisions(result: ShipmentApiScanResult) -> tuple[Any, ...]:
        provided = getattr(result, "audit_decisions", None)
        if provided is not None:
            return tuple(provided)

        report = result.report
        enqueued = {
            (
                item.system_order_no,
                item.platform_order_no,
                item.logistics_no,
            )
            for item in report.enqueued_candidates
        }
        decisions: list[dict[str, Any]] = [
            {
                "platform_order_no": item.platform_order_no,
                "system_order_no": item.system_order_no,
                "logistics_no": item.logistics_no,
                "source_page": item.source_page,
                "decision": (
                    "enqueued"
                    if (item.system_order_no, item.platform_order_no, item.logistics_no)
                    in enqueued
                    else "candidate"
                ),
                "reason_code": "shipment_tag_matched",
                "tag_matched": True,
            }
            for item in report.candidates
        ]
        decisions.extend(
            {
                "platform_order_no": item.platform_order_no,
                "system_order_no": item.system_order_no,
                "logistics_no": item.selected_logistics_no,
                "decision": "manual_review",
                "reason_code": item.reason,
            }
            for item in report.manual_reviews
        )
        decisions.extend(
            {
                "platform_order_no": item.platform_order_no,
                "system_order_no": item.system_order_no,
                "logistics_no": item.logistics_no,
                "decision": "duplicate",
                "reason_code": "duplicate_skipped",
                "duplicate": True,
            }
            for item in report.duplicate_skipped
        )
        decisions.extend(
            {
                "platform_order_no": item.platform_order_no,
                "system_order_no": item.system_order_no,
                "logistics_no": item.logistics_no,
                "decision": "manual_completed",
                "reason_code": "missing_from_complete_snapshot",
            }
            for item in report.manual_completed
        )
        return tuple(decisions)

    @staticmethod
    def _custom_audit_summary(
        result: CustomizationApiScanResult | None,
        payment_window_hours: int,
        *,
        status: str,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "status": status,
            "payment_window_hours": int(payment_window_hours),
        }
        if result is not None:
            summary.update(
                {
                    "complete": result.complete,
                    "order_count": result.api_raw_order_count,
                    "deduplicated_order_count": len(result.pagination.orders),
                    "row_count": result.row_count,
                    "candidate_count": result.candidate_count,
                    "processed_order_count": result.processed_order_count,
                    "skip_counts": result.skip_counts,
                    "pages_read": result.pagination.pages_read,
                    "expected_total": result.pagination.expected_total,
                    "diagnostic_codes": [item.code for item in result.diagnostics],
                }
            )
        return summary

    @staticmethod
    def _shipment_audit_summary(
        result: ShipmentApiScanResult | None,
        *,
        status: str,
        queue_total_count: int | None,
        query: Mapping[str, Any],
        email_preview_backfill_count: int,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "status": status,
            "scan_start_time": int(query["start_time"]),
            "scan_end_time": int(query["end_time"]),
            "window_count": int(query.get("window_count") or 0),
            "email_preview_backfill_count": int(email_preview_backfill_count),
        }
        if queue_total_count is not None:
            summary["queue_total_count"] = queue_total_count
        if result is not None:
            summary.update(
                {
                    "complete": result.complete,
                    "order_count": result.api_raw_order_count,
                    "deduplicated_order_count": len(result.pagination.orders),
                    "row_count": result.row_count,
                    "evaluable_row_count": result.evaluable_row_count,
                    "tagged_row_count": result.tagged_row_count,
                    "candidate_count": result.candidate_count,
                    "enqueued_count": result.enqueued_count,
                    "manual_completed_count": result.manual_completed_count,
                    "manual_review_count": result.manual_review_count,
                    "duplicate_count": result.report.duplicate_skipped_count,
                    "refreshed_count": result.report.refreshed_count,
                    "missing_critical_field_count": result.missing_critical_field_count,
                    "auto_paused_count": result.paused_count,
                    "auto_resumed_count": result.resumed_count,
                    "immediate_logistics_count": result.immediate_logistics_count,
                    "immediate_erp_count": result.immediate_erp_count,
                    "pages_read": result.pagination.pages_read,
                    "expected_total": result.pagination.expected_total,
                    "diagnostic_codes": [item.code for item in result.diagnostics],
                }
            )
        return summary

    @staticmethod
    def _shipment_payload_metrics(
        result: ShipmentApiScanResult | None,
        *,
        queue_total_count: int | None,
        query: Mapping[str, Any],
        email_preview_backfill_count: int,
        extra_diagnostic_codes: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        diagnostic_codes = (
            [item.code for item in result.diagnostics]
            if result is not None
            else []
        )
        diagnostic_codes.extend(extra_diagnostic_codes)
        base: dict[str, Any] = {
            "candidate_count": 0,
            "enqueued_count": 0,
            "manual_completed_count": 0,
            "row_count": 0,
            "evaluable_row_count": 0,
            # Temporary compatibility for older desktop consumers.  It now
            # means evaluable rows, never a payment-window count.
            "eligible_row_count": 0,
            "api_order_count": 0,
            "deduplicated_order_count": 0,
            "tagged_row_count": 0,
            "duplicate_skipped_count": 0,
            "refreshed_count": 0,
            "manual_review_count": 0,
            "missing_critical_field_count": 0,
            "auto_paused_count": 0,
            "auto_resumed_count": 0,
            "immediate_logistics_count": 0,
            "immediate_erp_count": 0,
            "email_preview_backfill_count": int(email_preview_backfill_count),
            "window_count": int(query.get("window_count") or 0),
            "scan_start_time": int(query["start_time"]),
            "scan_end_time": int(query["end_time"]),
            "queue_total_count": queue_total_count,
            "request_ids": [],
            "diagnostic_codes": list(dict.fromkeys(diagnostic_codes)),
        }
        if result is None:
            return base
        base.update(
            {
                "candidate_count": result.candidate_count,
                "enqueued_count": result.enqueued_count,
                "manual_completed_count": result.manual_completed_count,
                "row_count": result.row_count,
                "evaluable_row_count": result.evaluable_row_count,
                "eligible_row_count": result.evaluable_row_count,
                "api_order_count": result.api_raw_order_count,
                "deduplicated_order_count": len(result.pagination.orders),
                "tagged_row_count": result.tagged_row_count,
                "duplicate_skipped_count": result.report.duplicate_skipped_count,
                "refreshed_count": result.report.refreshed_count,
                "manual_review_count": result.manual_review_count,
                "missing_critical_field_count": result.missing_critical_field_count,
                "auto_paused_count": result.paused_count,
                "auto_resumed_count": result.resumed_count,
                "immediate_logistics_count": result.immediate_logistics_count,
                "immediate_erp_count": result.immediate_erp_count,
                "window_count": result.window_count,
                "request_ids": list(result.pagination.request_ids),
            }
        )
        return base

    def _custom_order_filters(self, hours: int) -> dict[str, Any]:
        """Return the Amazon-only pending-review slice used by customization."""

        return {
            **self._pending_review_payment_filters(hours),
            "platform_code": [10001],
        }

    @staticmethod
    def _shipment_order_filters(
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Return safe API windows for the current all-platform pending table.

        Lingxing requires a time range for a non-exact order query and limits
        each request window.  The desktop view is defined in China local time,
        so the logical range starts at 00:00 thirty calendar days ago and ends
        at 23:59:59 today.  Adjacent windows overlap by one second; the scanner
        removes the duplicate by the stable Lingxing order identity only after
        every window and page has been proven complete.
        """

        current = now or datetime.now(_CHINA_TIMEZONE)
        if current.tzinfo is None or current.utcoffset() is None:
            current = current.replace(tzinfo=_CHINA_TIMEZONE)
        else:
            current = current.astimezone(_CHINA_TIMEZONE)
        scan_start = (current - timedelta(days=_SHIPMENT_SCAN_DAYS)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        scan_end = current.replace(hour=23, minute=59, second=59, microsecond=0)
        start_timestamp = int(scan_start.timestamp())
        end_timestamp = int(scan_end.timestamp())

        windows: list[dict[str, Any]] = []
        window_start = start_timestamp
        while window_start < end_timestamp:
            window_end = min(
                window_start + _SHIPMENT_API_WINDOW_SECONDS,
                end_timestamp,
            )
            windows.append(
                {
                    "date_type": "global_purchase_time",
                    "start_time": window_start,
                    "end_time": window_end,
                    "order_status": 4,
                    "include_delete": False,
                }
            )
            if window_end >= end_timestamp:
                break
            window_start = window_end - _SHIPMENT_WINDOW_OVERLAP_SECONDS
        return tuple(windows)

    @staticmethod
    def _shipment_query_summary(
        windows: tuple[Mapping[str, Any], ...],
    ) -> dict[str, Any]:
        if not windows:
            raise ValueError("自动标发查询窗口不能为空。")
        return {
            "date_type": "global_purchase_time",
            "start_time": int(windows[0]["start_time"]),
            "end_time": int(windows[-1]["end_time"]),
            "order_status": 4,
            "include_delete": False,
            "window_count": len(windows),
        }

    @staticmethod
    def _pending_review_payment_filters(hours: int) -> dict[str, Any]:
        """Return shared documented bounds without imposing a platform."""

        now = datetime.now(timezone.utc)
        # The API uses an open interval.  The extra minute avoids losing orders
        # exactly on a boundary; both scanners reapply the exact confirmed
        # 96-hour rule after normalization.
        start = now - timedelta(hours=max(1, int(hours)), minutes=1)
        end = now + timedelta(minutes=1)
        return {
            "date_type": "global_payment_time",
            "start_time": int(start.timestamp()),
            "end_time": int(end.timestamp()),
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
        skip_text = "、".join(
            f"{code}={count}" for code, count in sorted(result.skip_counts.items())
        ) or "无"
        metrics = (
            f"API 读取 {result.api_raw_order_count} 个订单，规范化 {result.row_count} 行，"
            f"候选 {result.candidate_count} 个；跳过统计：{skip_text}。"
        )
        if result.state is ApiScanState.COMPLETE:
            return f"定制订单 API 扫描完成：{metrics}"
        return (
            "定制订单 API 快照不完整；未更新候选数据库，也未返回可操作订单。"
            f"{metrics}"
        )

    @staticmethod
    def _shipment_scan_message(
        result: ShipmentApiScanResult,
        queue_total_count: int | None,
        *,
        query: Mapping[str, Any],
        email_preview_backfill_count: int,
    ) -> str:
        queue_text = str(queue_total_count) if queue_total_count is not None else "读取失败"
        scan_start = datetime.fromtimestamp(
            int(query["start_time"]),
            _CHINA_TIMEZONE,
        ).strftime("%Y-%m-%d %H:%M:%S")
        scan_end = datetime.fromtimestamp(
            int(query["end_time"]),
            _CHINA_TIMEZONE,
        ).strftime("%Y-%m-%d %H:%M:%S")
        metrics = (
            f"购买时间范围 {scan_start} 至 {scan_end}（中国时区，{result.window_count} 个窗口），"
            f"API 原始读取 {result.api_raw_order_count} 行，跨窗口去重后 {len(result.pagination.orders)} 个订单，"
            f"规范化 {result.row_count} 行，"
            f"可判断 {result.evaluable_row_count} 行，"
            f"标签命中 {result.tagged_row_count} 行，候选 {result.candidate_count} 个，"
            f"本次新增 {result.enqueued_count} 个，重复 {result.report.duplicate_skipped_count} 个，"
            f"刷新 {result.report.refreshed_count} 个，人工检查 {result.manual_review_count} 个，"
            f"标签移除自动暂停 {result.paused_count} 个，标签恢复 {result.resumed_count} 个，"
            f"立即重试物流 {result.immediate_logistics_count} 个、ERP {result.immediate_erp_count} 个，"
            f"邮件预览补建或更新 {email_preview_backfill_count} 个，"
            f"当前队列共 {queue_text} 个。"
        )
        zero_explanation = (
            "本次新增为 0 只表示没有新任务，不代表当前队列为空。"
            if result.enqueued_count == 0
            else ""
        )
        if result.state is ApiScanState.COMPLETE:
            return f"自动标发 API 扫描完成：{metrics}{zero_explanation}"
        if result.state is ApiScanState.FAILED:
            return (
                "自动标发 API 扫描或本地更新失败；若错误发生在队列或邮件预览阶段，"
                "前面已成功提交的本地事务会保留，请按本次统计和完整日志核对后再重试。"
                f"{metrics}{zero_explanation}"
            )
        return (
            "自动标发 API 待审核快照不完整；未写入不完整快照中的候选，"
            f"也未暂停、恢复或结案已有任务。{metrics}{zero_explanation}"
        )


__all__ = ["DesktopApiServices", "build_capability_router"]
