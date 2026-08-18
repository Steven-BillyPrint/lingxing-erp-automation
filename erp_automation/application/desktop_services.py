"""Production API services wired into the desktop task runner.

The desktop worker creates a fresh OpenAPI client for every serialized task.
That keeps ``asyncio`` ownership simple, reloads edited encrypted credentials,
and guarantees the HTTP pool is closed before the next task begins.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import os
import re
import stat
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
    NOTIFICATION_SYNC_INCLUDE_DEFERRED_RETRIES_KEY,
)
from shipment_automation.queue_store import ShipmentQueueStore
from lingxing_automation.services.folder_builder import find_platform_order_folders
from lingxing_automation.products.catalog import PRODUCT_IDENTITY_CATALOG_VERSION

from .api_scanners import (
    ApiScanState,
    CustomizationApiScanResult,
    ShipmentApiScanResult,
    fetch_stable_order_snapshot,
    normalize_api_order_rows,
    read_order_product_type_details,
    receiver_email_from_payload,
    scan_customization_candidates,
    scan_shipment_candidates,
)
from .capabilities import (
    Capability as ApiCapability,
    CapabilityMode as ApiCapabilityMode,
    CapabilityRouter,
    CapabilityUnavailable,
)
from .readback import readback_delays_from_configuration
from .email_policy import email_preview_enabled
from .lingxing_gateway import LingxingGateway, OrderRecord, ResolvedOrderDetail
from .custom_order_api import (
    DEFAULT_WAREHOUSE_PROJECTION_DELAYS_SECONDS,
    LingxingCustomOrderApiOperations,
)


ClientFactory = Callable[[DesktopSettings], Awaitable[LingxingOpenAPIClient]]
PolicyProvider = Callable[[], CapabilityPolicy]


_SCAN_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_SHIPMENT_SCAN_DAYS = 30
_SHIPMENT_API_WINDOW_SECONDS = 30 * 24 * 60 * 60
_SHIPMENT_WINDOW_OVERLAP_SECONDS = 1
_ORDER_IDENTIFIER_LOOKUP_PAGE_LENGTH = 200
_ORDER_IDENTIFIER_LOOKUP_PAGE_LIMIT = 10
_PRODUCT_IDENTITY_BACKFILL_BATCH_SIZE = 25
_PRODUCT_IDENTITY_BACKFILL_TARGET_BUDGET = 500
_PRODUCT_IDENTITY_STATES = frozenset(
    {
        "product_identity_pending",
        "product_identity_tag_conflict",
        "product_identity_unrecognized",
        "product_identity_review",
    }
)
_PRODUCT_IDENTITY_RECORD_KEYS = frozenset(
    {
        "product_identity_state",
        "product_identity_status_text",
        "product_identity_last_error",
        "product_identity_last_checked_at",
        "product_identity_captured_at",
        "product_identity_detail_attempt_count",
        "product_identity_sku",
        "product_identity_paid_at",
        "product_identity_tag_text",
        "product_identity_observed_asins",
    }
)
_PLATFORM_ORDER_KEYS = frozenset(
    {
        "platformorderno",
        "platformorderid",
        "platformordername",
    }
)
_SYSTEM_ORDER_KEYS = (
    "global_order_no",
    "globalOrderNo",
    "system_order_no",
    "systemOrderNo",
    "order_number",
    "orderNumber",
)


def _identifier_text(value: object) -> str:
    return str(value or "").strip()


def _normalized_identifier_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _platform_order_nos_from_mapping(payload: Mapping[str, Any]) -> tuple[str, ...]:
    found: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if _normalized_identifier_key(key) in _PLATFORM_ORDER_KEYS:
                    text = _identifier_text(child)
                    if text:
                        found.append(text)
                visit(child)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for child in value:
                visit(child)

    visit(payload)
    return tuple(dict.fromkeys(found))


def _record_platform_order_nos(record: OrderRecord) -> tuple[str, ...]:
    values = [_identifier_text(record.order_number)]
    values.extend(_platform_order_nos_from_mapping(record.payload))
    return tuple(dict.fromkeys(value for value in values if value))


def _detail_system_order_no(payload: Mapping[str, Any]) -> str:
    for key in _SYSTEM_ORDER_KEYS:
        value = _identifier_text(payload.get(key))
        if value:
            return value
    return ""


def _record_system_order_no(record: OrderRecord) -> str:
    return _identifier_text(record.global_order_no) or _detail_system_order_no(
        record.payload
    )


_UI_TO_API_CAPABILITY: dict[UiCapability, tuple[ApiCapability, ...]] = {
    UiCapability.LIST_ORDERS: (ApiCapability.LIST_ORDERS,),
    UiCapability.GET_ORDER_DETAIL: (ApiCapability.GET_ORDER_DETAIL,),
    # The custom-order workflow writes both contact fields through the browser.
    # UPDATE_PHONE intentionally stays out of this UI mapping so its low-level
    # API method remains available to explicit diagnostics/compatibility callers.
    UiCapability.UPDATE_CONTACT: (ApiCapability.UPDATE_BUYER_EMAIL,),
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
    UiCapability.SEND_NOTIFICATION: (ApiCapability.SEND_EMAIL,),
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

    # Buyer e-mail has no API implementation.  Contact writeback itself is
    # deliberately browser-only in the custom-order orchestration, while the
    # low-level phone API remains available for diagnostics and compatibility.
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
            runtime_options: dict[str, Any] = {}
            backend = self.configuration_store.backend
            if getattr(backend, "name", "") == "host-key-aes-256-gcm":
                local_state = self.workspace / "data" / "local"
                runtime_options = {
                    "token_path": local_state / "lingxing-token.enc",
                    "lock_path": local_state / "lingxing-token.lock",
                    "token_backend": backend,
                }
            client = await create_lingxing_openapi_client(
                self.configuration_store,
                base_url=settings.lingxing_api_base_url,
                timeout=float(settings.api_timeout_seconds),
                **runtime_options,
            )
        policy = self.policy_provider()
        router = build_capability_router(
            policy,
            writes_enabled_provider=lambda: not self.policy_provider().emergency_stop_writes,
        )
        return LingxingGateway(client, router), client

    async def get_order_detail_payload(
        self,
        settings: DesktopSettings,
        order_identifier: str,
    ) -> ResolvedOrderDetail:
        """Resolve a system/platform order number and fetch one verified detail."""

        gateway, client = await self.create_gateway(settings)
        try:
            normalized = _identifier_text(order_identifier)
            if not normalized:
                raise CapabilityUnavailable("订单号不能为空。")
            direct_error: CapabilityUnavailable | None = None
            if normalized.isdecimal():
                try:
                    return await self._resolved_system_order_detail(
                        gateway,
                        requested_order_no=normalized,
                        system_order_no=normalized,
                    )
                except CapabilityUnavailable as exc:
                    # Some marketplaces use digits-only platform numbers.  A
                    # failed direct detail lookup may therefore still be
                    # resolved through the documented platform-order filter.
                    direct_error = exc

            records: list[OrderRecord] = []
            offset = 0
            for _ in range(_ORDER_IDENTIFIER_LOOKUP_PAGE_LIMIT):
                page = await gateway.list_orders(
                    offset=offset,
                    length=_ORDER_IDENTIFIER_LOOKUP_PAGE_LENGTH,
                    filters={"platform_order_nos": [normalized]},
                )
                records.extend(page.items)
                if page.next_offset is None:
                    break
                offset = page.next_offset
            else:
                raise CapabilityUnavailable(
                    "按平台单号查询领星订单超过安全分页上限，请输入领星系统单号。"
                )

            unique_records: dict[tuple[str, tuple[str, ...]], OrderRecord] = {}
            for record in records:
                unique_records.setdefault(
                    (
                        _record_system_order_no(record),
                        _record_platform_order_nos(record),
                    ),
                    record,
                )
            exact = [
                record
                for record in unique_records.values()
                if normalized in _record_platform_order_nos(record)
            ]
            if not exact and len(unique_records) == 1:
                only = next(iter(unique_records.values()))
                if not _record_platform_order_nos(only):
                    # The documented platform filter is authoritative even when
                    # an older response shape omits the echoed platform number.
                    exact = [only]
            system_order_nos = tuple(
                dict.fromkeys(
                    value
                    for record in exact
                    if (value := _record_system_order_no(record))
                )
            )
            if not system_order_nos:
                if direct_error is not None:
                    raise direct_error
                raise CapabilityUnavailable(
                    f"领星 API 未找到平台单号 {normalized}，请核对订单号或输入领星系统单号。"
                )
            if len(system_order_nos) != 1:
                visible = "、".join(system_order_nos[:8])
                suffix = "……" if len(system_order_nos) > 8 else ""
                raise CapabilityUnavailable(
                    f"平台单号 {normalized} 对应多个领星系统单号（{visible}{suffix}），"
                    "无法安全判断下单对象，请输入需要处理的系统单号。"
                )
            return await self._resolved_system_order_detail(
                gateway,
                requested_order_no=normalized,
                system_order_no=system_order_nos[0],
                expected_platform_order_no=normalized,
            )
        finally:
            await client.aclose()

    @staticmethod
    async def _resolved_system_order_detail(
        gateway: LingxingGateway,
        *,
        requested_order_no: str,
        system_order_no: str,
        expected_platform_order_no: str = "",
    ) -> ResolvedOrderDetail:
        detail = await gateway.get_order_detail(system_order_no)
        payload = dict(detail.payload)
        observed_system_order_no = _detail_system_order_no(payload)
        if observed_system_order_no and observed_system_order_no != system_order_no:
            raise CapabilityUnavailable(
                "领星订单详情返回的系统单号与请求不一致，已停止以避免填写错误订单。"
            )
        platform_order_nos = _platform_order_nos_from_mapping(payload)
        if (
            expected_platform_order_no
            and platform_order_nos
            and expected_platform_order_no not in platform_order_nos
        ):
            raise CapabilityUnavailable(
                "领星订单详情返回的平台单号与请求不一致，已停止以避免填写错误订单。"
            )
        if len(platform_order_nos) > 1:
            raise CapabilityUnavailable(
                "同一领星系统订单包含多个平台单号，无法安全填写唯一客户订单号，"
                "请人工处理。"
            )
        return ResolvedOrderDetail(
            requested_order_no=requested_order_no,
            system_order_no=system_order_no,
            platform_order_no=(
                expected_platform_order_no
                or (platform_order_nos[0] if platform_order_nos else "")
            ),
            payload=payload,
        )

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

        gateway, client = await self.create_gateway(settings)
        try:
            yield LingxingCustomOrderApiOperations(
                gateway,
                verification_delays_seconds=readback_delays_from_configuration(
                    configuration
                ),
                warehouse_projection_delays_seconds=(
                    DEFAULT_WAREHOUSE_PROJECTION_DELAYS_SECONDS
                ),
                high_value_split_weight_kg=int(
                    configuration.get(
                        "automation.high_value_split_weight_kg",
                        settings.high_value_split_weight_kg,
                    )
                ),
            )
        finally:
            await client.aclose()

    async def get_custom_order_processing_status(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        platform_order_no: str,
        system_order_no: str,
    ):
        """Read one exact order for the desktop pre-write cancellation gate."""

        async with self.custom_order_operations(settings, configuration) as operations:
            return await operations.get_order_processing_status(
                platform_order_no=platform_order_no,
                system_order_no=system_order_no,
            )

    async def scan_custom_orders(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        task_id: str | None = None,
        operator_name: str = "",
        operator_email: str = "",
    ) -> Mapping[str, int]:
        del configuration  # credentials are read directly from config.enc
        audit_task_id = self._scan_task_id(task_id, scan_kind="customization")
        started_at = datetime.now(timezone.utc)
        filters = self._custom_order_filters(settings.payment_window_hours)
        store: CustomWorkflowStore | None = None
        result: CustomizationApiScanResult | None = None
        cancellation_pagination = None
        cancellation_order_nos: dict[str, str] = {}
        cancellation_decisions: tuple[Mapping[str, Any], ...] = ()
        reconciled_cancelled_count = 0
        reactivation_decisions: tuple[Mapping[str, Any], ...] = ()
        buyer_cancel_clear_observed_count = 0
        buyer_cancel_reactivated_count = 0
        buyer_cancel_clear_reset_count = 0
        reactivation_reconciled = False
        folder_reconciliation_decisions: tuple[Mapping[str, Any], ...] = ()
        folder_reconciliation_state = "not_started"
        missing_candidate_count = 0
        folder_reconciled_completed_count = 0
        folder_reconciled_pending_count = 0
        folder_reconciliation_changed_count = 0
        folder_reconciliation_error_preserved_count = 0
        folder_reconciliation_diagnostic_codes: list[str] = []
        try:
            store = CustomWorkflowStore(self._path(settings.custom_state_path))
            reactivation_order_nos = store.buyer_cancel_reactivation_order_nos()
            pending_product_identities = (
                store.list_product_identity_pending_workflows()
            )
            pending_identity_order_nos = {
                str(item.get("platform_order_no") or "").strip()
                for item in pending_product_identities
                if str(item.get("platform_order_no") or "").strip()
            }
            historical_identity_backfill = [
                item
                for item in store.list_missing_product_type_workflows(
                    catalog_version=PRODUCT_IDENTITY_CATALOG_VERSION,
                )
                if str(item.get("platform_order_no") or "").strip()
                not in pending_identity_order_nos
            ]
            gateway, client = await self.create_gateway(settings)
            try:
                result = await scan_customization_candidates(
                    gateway,
                    store,
                    filters=filters,
                    reactivation_order_nos=reactivation_order_nos,
                    pending_product_identities=(
                        *pending_product_identities,
                        *historical_identity_backfill,
                    ),
                )
                # Buyer cancellation is a system-processing tag.  Lingxing
                # removes such rows from the pending-review filtered result,
                # so a second complete 96-hour Amazon snapshot is required to
                # reconcile workflows that were queued by an earlier scan.
                cancellation_filters = dict(filters)
                cancellation_filters.pop("order_status", None)
                cancellation_pagination = await fetch_stable_order_snapshot(
                    gateway,
                    filters=cancellation_filters,
                )
                normalized_cancellations = normalize_api_order_rows(
                    cancellation_pagination
                )
                for row in normalized_cancellations.customization_rows:
                    if not bool(row.get("buyer_cancel_requested")):
                        continue
                    platform_order_no = str(row.get("platform_order_no") or "").strip()
                    system_order_no = str(row.get("system_order_no") or "").strip()
                    if platform_order_no:
                        cancellation_order_nos.setdefault(
                            platform_order_no,
                            system_order_no,
                        )
            finally:
                await client.aclose()
            if result.complete:
                self._persist_custom_candidates(store, result)
                observed_identity_order_nos = {
                    str(item.get("platform_order_no") or "").strip()
                    for item in result.observed_workflows
                    if str(item.get("platform_order_no") or "").strip()
                }
                store.mark_product_identity_backfill_attempts(
                    (
                        str(item.get("platform_order_no") or "")
                        for item in historical_identity_backfill
                        if str(item.get("platform_order_no") or "").strip()
                        in observed_identity_order_nos
                    ),
                    catalog_version=PRODUCT_IDENTITY_CATALOG_VERSION,
                )
            if cancellation_order_nos:
                cancellation_summary = store.mark_workflows_not_required(
                    cancellation_order_nos,
                    reason="领星订单状态显示买家申请取消，定制流程不再需要处理。",
                    actor="api_scanner",
                )
                reconciled_cancelled_count = cancellation_summary.changed_order_count
                cancellation_decisions = tuple(
                    {
                        "platform_order_no": platform_order_no,
                        "system_order_no": system_order_no,
                        "paid_at": "",
                        "decision": (
                            "not_required"
                            if (
                                (store.get_workflow(platform_order_no) or {}).get(
                                    "workflow_status"
                                )
                                == "not_required"
                            )
                            else "excluded"
                        ),
                        "reason_code": "buyer_cancel_requested",
                        "custom_tag_text": "",
                        "items": [],
                    }
                    for platform_order_no, system_order_no in cancellation_order_nos.items()
                )

            reactivation_candidate_by_order = {
                candidate.platform_order_no: candidate
                for candidate in result.reactivation_candidates
            }
            reactivation_summary = store.reconcile_buyer_cancel_reactivation(
                scan_id=audit_task_id,
                eligible_order_nos=reactivation_candidate_by_order,
                currently_cancelled_order_nos=cancellation_order_nos,
                snapshots_complete=(
                    result.complete
                    and cancellation_pagination is not None
                    and cancellation_pagination.complete
                ),
                actor="api_scanner",
            )
            reactivation_reconciled = True
            buyer_cancel_clear_observed_count = (
                reactivation_summary.clear_observed_count
            )
            buyer_cancel_reactivated_count = reactivation_summary.reactivated_count
            buyer_cancel_clear_reset_count = reactivation_summary.reset_count
            reactivation_decisions = tuple(
                [
                    {
                        "platform_order_no": order_no,
                        "system_order_no": str(
                            getattr(
                                reactivation_candidate_by_order.get(order_no),
                                "system_order_no",
                                "",
                            )
                            or ""
                        ),
                        "paid_at": str(
                            getattr(
                                reactivation_candidate_by_order.get(order_no),
                                "paid_at_text",
                                "",
                            )
                            or ""
                        ),
                        "decision": "pending_confirmation",
                        "reason_code": "buyer_cancel_clear_observed",
                        "custom_tag_text": "",
                        "items": [],
                    }
                    for order_no in reactivation_summary.clear_observed_order_nos
                ]
                + [
                    {
                        "platform_order_no": order_no,
                        "system_order_no": str(
                            getattr(
                                reactivation_candidate_by_order.get(order_no),
                                "system_order_no",
                                "",
                            )
                            or ""
                        ),
                        "paid_at": str(
                            getattr(
                                reactivation_candidate_by_order.get(order_no),
                                "paid_at_text",
                                "",
                            )
                            or ""
                        ),
                        "decision": "reactivated",
                        "reason_code": "buyer_cancel_request_cleared_reactivated",
                        "custom_tag_text": "",
                        "items": [],
                    }
                    for order_no in reactivation_summary.reactivated_order_nos
                ]
                + [
                    {
                        "platform_order_no": order_no,
                        "system_order_no": "",
                        "paid_at": "",
                        "decision": "not_required",
                        "reason_code": "buyer_cancel_clear_reset",
                        "custom_tag_text": "",
                        "items": [],
                    }
                    for order_no in reactivation_summary.reset_order_nos
                ]
            )

            if (
                result.complete
                and cancellation_pagination is not None
                and cancellation_pagination.complete
            ):
                candidate_order_nos = {
                    candidate.platform_order_no
                    for candidate in (
                        *result.candidates,
                        *result.reactivation_candidates,
                    )
                }
                active_workflows = store.list_active_scanned_workflows()
                missing_workflows = [
                    workflow
                    for workflow in active_workflows
                    if str(workflow.get("platform_order_no") or "").strip()
                    not in candidate_order_nos
                ]
                missing_candidate_count = len(missing_workflows)
                protected_missing_workflows = [
                    workflow
                    for workflow in missing_workflows
                    if bool(workflow.get("folder_reconciliation_protected"))
                ]
                reconcilable_missing_workflows = [
                    workflow
                    for workflow in missing_workflows
                    if not bool(workflow.get("folder_reconciliation_protected"))
                ]
                folder_reconciliation_error_preserved_count = len(
                    protected_missing_workflows
                )
                protected_decisions = tuple(
                    {
                        "platform_order_no": str(
                            workflow.get("platform_order_no") or ""
                        ).strip(),
                        "system_order_no": str(
                            workflow.get("original_system_order_no") or ""
                        ).strip(),
                        "paid_at": "",
                        "decision": "manual_review",
                        "reason_code": "missing_candidate_existing_error_preserved",
                        "custom_tag_text": "",
                        "items": [],
                    }
                    for workflow in protected_missing_workflows
                )
                if not reconcilable_missing_workflows:
                    folder_reconciliation_state = "complete"
                    folder_reconciliation_decisions = protected_decisions
                else:
                    try:
                        folder_states, unresolved_order_nos = (
                            self._find_missing_candidate_order_folders(
                                settings.folder_root,
                                reconcilable_missing_workflows,
                                payment_window_hours=settings.payment_window_hours,
                            )
                        )
                    except OSError:
                        folder_reconciliation_state = "unavailable"
                        folder_reconciliation_diagnostic_codes.append(
                            "missing_candidate_folder_root_unavailable"
                        )
                        unavailable_decisions = tuple(
                            {
                                "platform_order_no": str(
                                    workflow.get("platform_order_no") or ""
                                ).strip(),
                                "system_order_no": str(
                                    workflow.get("original_system_order_no") or ""
                                ).strip(),
                                "paid_at": "",
                                "decision": "manual_review",
                                "reason_code": "folder_root_unavailable",
                                "custom_tag_text": "",
                                "items": [],
                            }
                            for workflow in reconcilable_missing_workflows
                        )
                        folder_reconciliation_decisions = (
                            *protected_decisions,
                            *unavailable_decisions,
                        )
                    else:
                        folder_reconciliation_state = (
                            "complete" if not unresolved_order_nos else "incomplete"
                        )
                        if unresolved_order_nos:
                            folder_reconciliation_diagnostic_codes.append(
                                "missing_candidate_folder_anchor_missing"
                            )
                        if folder_states:
                            folder_summary = store.reconcile_missing_candidate_folders(
                                folder_states,
                                reason=(
                                    "Order disappeared from the next complete custom-candidate "
                                    "snapshot; reconciled against the platform-order folder."
                                ),
                                actor="api_scanner",
                            )
                            folder_reconciled_completed_count = (
                                folder_summary.completed_count
                            )
                            folder_reconciled_pending_count = folder_summary.pending_count
                            folder_reconciliation_changed_count = (
                                folder_summary.changed_order_count
                            )
                            folder_reconciliation_error_preserved_count += (
                                folder_summary.error_preserved_count
                            )
                        workflow_by_order = {
                            str(workflow.get("platform_order_no") or "").strip(): workflow
                            for workflow in reconcilable_missing_workflows
                        }
                        resolved_decisions = [
                            {
                                "platform_order_no": order_no,
                                "system_order_no": str(
                                    workflow_by_order[order_no].get(
                                        "original_system_order_no"
                                    )
                                    or ""
                                ).strip(),
                                "paid_at": "",
                                "decision": "completed" if found else "pending",
                                "reason_code": (
                                    "missing_candidate_folder_found"
                                    if found
                                    else "missing_candidate_folder_absent"
                                ),
                                "matched": found,
                                "custom_tag_text": "",
                                "items": [],
                            }
                            for order_no, found in folder_states.items()
                        ]
                        unresolved_decisions = [
                            {
                                "platform_order_no": order_no,
                                "system_order_no": str(
                                    workflow_by_order[order_no].get(
                                        "original_system_order_no"
                                    )
                                    or ""
                                ).strip(),
                                "paid_at": "",
                                "decision": "manual_review",
                                "reason_code": "folder_search_anchor_missing",
                                "custom_tag_text": "",
                                "items": [],
                            }
                            for order_no in unresolved_order_nos
                        ]
                        folder_reconciliation_decisions = tuple(
                            [
                                *protected_decisions,
                                *resolved_decisions,
                                *unresolved_decisions,
                            ]
                        )
            else:
                folder_reconciliation_state = "skipped_incomplete_snapshot"
                folder_reconciliation_diagnostic_codes.append(
                    "missing_candidate_folder_reconciliation_snapshot_incomplete"
                )
        except Exception as error:
            if store is not None and not reactivation_reconciled:
                try:
                    reset_summary = store.reconcile_buyer_cancel_reactivation(
                        scan_id=audit_task_id,
                        eligible_order_nos=(),
                        currently_cancelled_order_nos=cancellation_order_nos,
                        snapshots_complete=False,
                        actor="api_scanner",
                    )
                except Exception:
                    # Preserve the original scan failure in the task result.
                    # A reset write can only fail when the state database is
                    # itself unavailable; surface that separately in audit.
                    folder_reconciliation_diagnostic_codes.append(
                        "buyer_cancel_clear_reset_failed"
                    )
                else:
                    reactivation_reconciled = True
                    buyer_cancel_clear_reset_count = reset_summary.reset_count
                    reactivation_decisions = tuple(
                        {
                            "platform_order_no": order_no,
                            "system_order_no": "",
                            "paid_at": "",
                            "decision": "not_required",
                            "reason_code": "buyer_cancel_clear_reset",
                            "custom_tag_text": "",
                            "items": [],
                        }
                        for order_no in reset_summary.reset_order_nos
                    )
            return self._failed_scan_payload(
                settings=settings,
                task_id=audit_task_id,
                scan_kind="customization",
                started_at=started_at,
                operator_name=operator_name,
                operator_email=operator_email,
                query=filters,
                error=error,
                pages=(
                    *(
                        result.pagination.page_traces
                        if result is not None
                        else ()
                    ),
                    *(
                        cancellation_pagination.page_traces
                        if cancellation_pagination is not None
                        else ()
                    ),
                ),
                order_decisions=(
                    (
                        *self._custom_audit_decisions(result),
                        *cancellation_decisions,
                        *reactivation_decisions,
                        *folder_reconciliation_decisions,
                    )
                    if result is not None
                    else (
                        *cancellation_decisions,
                        *reactivation_decisions,
                        *folder_reconciliation_decisions,
                    )
                ),
                summary={
                    **self._custom_audit_summary(
                        result,
                        settings.payment_window_hours,
                        status="failed",
                    ),
                    "buyer_cancel_detected_count": len(cancellation_order_nos),
                    "buyer_cancel_reconciled_count": reconciled_cancelled_count,
                    "buyer_cancel_clear_observed_count": buyer_cancel_clear_observed_count,
                    "buyer_cancel_reactivated_count": buyer_cancel_reactivated_count,
                    "buyer_cancel_clear_reset_count": buyer_cancel_clear_reset_count,
                    "buyer_cancel_snapshot_state": (
                        str(cancellation_pagination.state)
                        if cancellation_pagination is not None
                        else "not_started"
                    ),
                    "missing_candidate_count": missing_candidate_count,
                    "folder_reconciled_completed_count": folder_reconciled_completed_count,
                    "folder_reconciled_pending_count": folder_reconciled_pending_count,
                    "folder_reconciliation_error_preserved_count": (
                        folder_reconciliation_error_preserved_count
                    ),
                    "folder_reconciliation_changed_count": folder_reconciliation_changed_count,
                    "folder_reconciliation_state": folder_reconciliation_state,
                },
                payload_defaults={
                    "custom_orders": [],
                    "candidate_count": 0,
                    "row_count": 0,
                    "api_order_count": 0,
                    "processed_order_count": 0,
                    "skip_counts": {},
                    "payment_window_hours": int(settings.payment_window_hours),
                    "request_ids": [],
                    "diagnostic_codes": [
                        "scan_runtime_failure",
                        *folder_reconciliation_diagnostic_codes,
                    ],
                    "buyer_cancel_clear_observed_count": buyer_cancel_clear_observed_count,
                    "buyer_cancel_reactivated_count": buyer_cancel_reactivated_count,
                    "buyer_cancel_clear_reset_count": buyer_cancel_clear_reset_count,
                    "missing_candidate_count": missing_candidate_count,
                    "folder_reconciled_completed_count": folder_reconciled_completed_count,
                    "folder_reconciled_pending_count": folder_reconciled_pending_count,
                    "folder_reconciliation_error_preserved_count": (
                        folder_reconciliation_error_preserved_count
                    ),
                    "folder_reconciliation_changed_count": folder_reconciliation_changed_count,
                    "folder_reconciliation_state": folder_reconciliation_state,
                },
            )

        rows = (
            [
                {
                    "platform_order_no": candidate.platform_order_no,
                    "system_order_no": candidate.system_order_no,
                    "product_type": " | ".join(candidate.product_types),
                    "product_types": list(candidate.product_types),
                    "workflow_stage": "candidate",
                    "status_text": "待处理",
                    "last_error": "",
                }
                for candidate in result.candidates
            ]
            + [
                {
                    "platform_order_no": observation.platform_order_no,
                    "system_order_no": observation.system_order_no,
                    "product_type": " | ".join(observation.product_types),
                    "product_types": list(observation.product_types),
                    "workflow_stage": observation.state,
                    "status_text": observation.state,
                    "last_error": observation.last_error,
                }
                for observation in result.product_identity_observations
            ]
            if result.complete
            else []
        )
        scan_status = self._task_status(result.state)
        if scan_status == "completed" and (
            cancellation_pagination is None
            or not cancellation_pagination.complete
            or folder_reconciliation_state != "complete"
        ):
            scan_status = "incomplete"
        diagnostic_codes = list(
            dict.fromkeys(
                [
                    *[item.code for item in result.diagnostics],
                    *(
                        [item.code for item in cancellation_pagination.diagnostics]
                        if cancellation_pagination is not None
                        else []
                    ),
                    *(
                        []
                        if cancellation_pagination is not None
                        and cancellation_pagination.complete
                        else ["buyer_cancel_reconciliation_snapshot_incomplete"]
                    ),
                    *folder_reconciliation_diagnostic_codes,
                ]
            )
        )
        payload: dict[str, Any] = {
            "status": scan_status,
            "message": (
                self._custom_scan_message(result)
                + (
                    f" 已将 {reconciled_cancelled_count} 张买家申请取消的已入队订单改为不需要。"
                    if reconciled_cancelled_count
                    else ""
                )
                + (
                    f" 已确认 {buyer_cancel_clear_observed_count} 张订单的取消申请首次消失，"
                    "等待下一次完整扫描确认。"
                    if buyer_cancel_clear_observed_count
                    else ""
                )
                + (
                    f" 取消申请已撤销，{buyer_cancel_reactivated_count} 张订单已重新入队。"
                    if buyer_cancel_reactivated_count
                    else ""
                )
                + (
                    " 消失候选文件夹对账："
                    f"已完成 {folder_reconciled_completed_count}，"
                    f"待处理 {folder_reconciled_pending_count}，"
                    "保留报错 "
                    f"{folder_reconciliation_error_preserved_count}。"
                    if missing_candidate_count
                    and folder_reconciliation_state in {"complete", "incomplete"}
                    else ""
                )
                + (
                    " 文件夹根目录当前不可读取，消失候选未改状态；"
                    "请打开详细扫描日志检查。"
                    if folder_reconciliation_state == "unavailable"
                    else ""
                )
                + (
                    " 本轮对账快照不完整，未对无法确认的消失候选改状态。"
                    if folder_reconciliation_state
                    in {"incomplete", "skipped_incomplete_snapshot"}
                    else ""
                )
            ),
            "custom_orders": rows,
            "candidate_count": result.candidate_count,
            "row_count": result.row_count,
            "api_order_count": len(result.pagination.orders),
            "processed_order_count": result.processed_order_count,
            "skip_counts": dict(result.skip_counts),
            "payment_window_hours": int(result.payment_window_hours),
            "request_ids": list(
                dict.fromkeys(
                    [
                        *result.pagination.request_ids,
                        *result.detail_request_ids,
                        *(
                            cancellation_pagination.request_ids
                            if cancellation_pagination is not None
                            else ()
                        ),
                    ]
                )
            ),
            "diagnostic_codes": diagnostic_codes,
            "buyer_cancel_detected_count": len(cancellation_order_nos),
            "buyer_cancel_reconciled_count": reconciled_cancelled_count,
            "buyer_cancel_clear_observed_count": buyer_cancel_clear_observed_count,
            "buyer_cancel_reactivated_count": buyer_cancel_reactivated_count,
            "buyer_cancel_clear_reset_count": buyer_cancel_clear_reset_count,
            "missing_candidate_count": missing_candidate_count,
            "folder_reconciled_completed_count": folder_reconciled_completed_count,
            "folder_reconciled_pending_count": folder_reconciled_pending_count,
            "folder_reconciliation_error_preserved_count": (
                folder_reconciliation_error_preserved_count
            ),
            "folder_reconciliation_changed_count": folder_reconciliation_changed_count,
            "folder_reconciliation_state": folder_reconciliation_state,
            "product_identity_pending_count": (
                result.product_identity_pending_count
            ),
        }
        return self._complete_scan_payload(
            settings=settings,
            task_id=audit_task_id,
            scan_kind="customization",
            started_at=started_at,
            operator_name=operator_name,
            operator_email=operator_email,
            query=filters,
            pages=(
                *result.pagination.page_traces,
                *(
                    cancellation_pagination.page_traces
                    if cancellation_pagination is not None
                    else ()
                ),
            ),
            order_decisions=(
                *self._custom_audit_decisions(result),
                *cancellation_decisions,
                *reactivation_decisions,
                *folder_reconciliation_decisions,
            ),
            summary={
                **self._custom_audit_summary(
                    result,
                    settings.payment_window_hours,
                    status=payload["status"],
                ),
                "diagnostic_codes": diagnostic_codes,
                "buyer_cancel_detected_count": len(cancellation_order_nos),
                "buyer_cancel_reconciled_count": reconciled_cancelled_count,
                "buyer_cancel_clear_observed_count": buyer_cancel_clear_observed_count,
                "buyer_cancel_reactivated_count": buyer_cancel_reactivated_count,
                "buyer_cancel_clear_reset_count": buyer_cancel_clear_reset_count,
                "buyer_cancel_snapshot_state": (
                    str(cancellation_pagination.state)
                    if cancellation_pagination is not None
                    else "not_started"
                ),
                "missing_candidate_count": missing_candidate_count,
                "folder_reconciled_completed_count": folder_reconciled_completed_count,
                "folder_reconciled_pending_count": folder_reconciled_pending_count,
                "folder_reconciliation_error_preserved_count": (
                    folder_reconciliation_error_preserved_count
                ),
                "folder_reconciliation_changed_count": folder_reconciliation_changed_count,
                "folder_reconciliation_state": folder_reconciliation_state,
            },
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

    @staticmethod
    def _notification_sync_summary_text(report: Mapping[str, Any]) -> str:
        discovery_error_count = int(report.get("discovery_error_count") or 0)
        duration_seconds = float(report.get("total_duration_ms") or 0) / 1000
        api_call_count = sum(
            int(report.get(key) or 0)
            for key in (
                "order_discovery_api_call_count",
                "order_facts_api_call_count",
                "wms_api_call_count",
            )
        )
        performance_detail = (
            f"同步耗时 {duration_seconds:.1f} 秒，订单/WMS API 请求 {api_call_count} 次。"
        )
        discovery_detail = ""
        if discovery_error_count:
            details = [
                str(report.get("discovery_error_type") or "Exception"),
            ]
            if report.get("discovery_error_http_status") is not None:
                details.append(
                    f"HTTP {report.get('discovery_error_http_status')}"
                )
            if report.get("discovery_error_api_code") is not None:
                details.append(
                    f"API {report.get('discovery_error_api_code')}"
                )
            if report.get("discovery_error_request_id"):
                details.append(
                    f"请求 {report.get('discovery_error_request_id')}"
                )
            if report.get("discovery_error_id"):
                details.append(f"错误编号 {report.get('discovery_error_id')}")
            discovery_detail = (
                f"订单发现异常 {discovery_error_count}（"
                + "，".join(details)
                + "）。"
            )
        return (
            "客户通知："
            f"新增草稿 {int(report.get('new_draft_count') or 0)}、"
            f"待补物流 {int(report.get('partial_logistics_order_count') or 0)}、"
            f"等待物流 {int(report.get('waiting_logistics_order_count') or 0)}、"
            f"等待出库 {int(report.get('waiting_outbound_order_count') or 0)} 单/"
            f"{int(report.get('waiting_outbound_package_count') or 0)} 包裹、"
            f"未知出库状态 {int(report.get('unknown_outbound_status_count') or 0)}、"
            f"WMS 状态冲突 {int(report.get('conflicting_wms_status_count') or 0)}、"
            f"已阻止旧通知 {int(report.get('blocked_existing_notification_count') or 0)}、"
            f"无变化 {int(report.get('unchanged_order_count') or 0)}、"
            f"失败 {int(report.get('failed_order_count') or 0)}、"
            f"姓名冲突 {int(report.get('recipient_name_conflict_count') or 0)}、"
            f"政策遮罩已排除 "
            f"{int(report.get('recipient_name_policy_masked_count') or 0)}、"
            f"历史姓名复用 "
            f"{int(report.get('recipient_name_selection_reused_count') or 0)}、"
            f"姓名弹窗 "
            f"{int(report.get('recipient_name_selection_prompt_count') or 0)}、"
            f"重试失败告警 {int(report.get('recipient_name_retry_alert_count') or 0)}。"
            + discovery_detail
            + performance_detail
        )

    async def refresh_shipment_notification_contacts(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        task_id: str | None = None,
        notification_ids: Sequence[int] | None = None,
    ) -> Mapping[str, Any]:
        """Refresh selected notification contacts from local customization JSON."""

        del task_id
        from erp_automation.application.notification_contact_refresh import (
            refresh_shipment_notification_contacts,
        )
        from erp_automation.persistence import CustomWorkflowStore
        from shipment_automation.notification_domain import NotificationConfiguration
        from shipment_automation.notification_store import ShipmentNotificationStore

        ShipmentQueueStore(self._path(settings.queue_path)).initialize()
        store = ShipmentNotificationStore(self._path(settings.queue_path))
        summary = await refresh_shipment_notification_contacts(
            store,
            NotificationConfiguration.from_mapping(configuration),
            tuple(notification_ids or ()),
            workflow_store=CustomWorkflowStore(
                self._path(settings.custom_state_path)
            ),
            folder_root=self._path(settings.folder_root),
            staging_root=self.workspace / "logs" / "custom_zip_staging",
        )
        report = summary.to_mapping()
        warnings = (
            int(report.get("no_usable_count") or 0)
            + int(report.get("conflict_count") or 0)
            + int(report.get("failed_count") or 0)
        )
        reason_labels = {
            "workflow_missing": "没有工作流记录且缺少可用日期",
            "workflow_date_missing": "工作流和通知都缺少可用日期",
            "folder_missing": "未找到对应订单文件夹或 ZIP staging",
            "json_missing": "订单目录中没有 JSON",
            "order_mismatch": "JSON 内的平台单号不匹配",
            "contact_fields_missing": "JSON 中没有支持的联系方式问题",
            "authoritative_empty": "客户未填写联系方式",
            "ambiguous": "JSON 中存在多组不同联系方式",
            "parse_error": "JSON 无法解析",
            "read_failed": "读取 JSON 失败",
        }
        issue_details: list[str] = []
        for item in report.get("results") or ():
            if not isinstance(item, Mapping):
                continue
            if str(item.get("status") or "") in {"refreshed", "unchanged"}:
                continue
            platform = str(item.get("platform_order_no") or "-").strip() or "-"
            code = str(item.get("json_status") or item.get("status") or "").strip()
            issue_details.append(f"{platform}（{reason_labels.get(code, '未取得可用联系方式')}）")
        issue_suffix = (
            " 未取得明细：" + "、".join(issue_details[:5]) + "。"
            if issue_details
            else ""
        )
        return {
            "status": "completed_with_warnings" if warnings else "completed",
            "message": (
                "联系方式重新获取完成："
                f"更新 {int(report.get('refreshed_count') or 0)}，"
                f"无变化 {int(report.get('unchanged_count') or 0)}，"
                f"未取得有效值 {int(report.get('no_usable_count') or 0)}，"
                f"存在冲突 {int(report.get('conflict_count') or 0)}，"
                f"失败 {int(report.get('failed_count') or 0)}，"
                f"新待审核版本 {int(report.get('new_review_count') or 0)}。"
                "本次只读取本地订单文件夹中的定制 JSON 并更新本地草稿，"
                "未调用领星接口，未写入 ERP，未发送邮件或短信。"
                + issue_suffix
            ),
            "contact_refresh": report,
            "external_provider_calls": 0,
            "erp_write_calls": 0,
        }

    async def sync_shipment_notifications(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        task_id: str | None = None,
        platform_order_nos: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        """Refresh notification WMS facts without running Alibaba logistics."""

        from shipment_automation.notification_domain import NotificationConfiguration
        from shipment_automation.notification_store import ShipmentNotificationStore
        from shipment_automation.notification_sync import sync_notification_drafts

        # The notification tables share the shipment queue database.  A manual
        # scan with zero candidates may reach this service before the queue has
        # ever been initialized, so create the core read model first.
        ShipmentQueueStore(self._path(settings.queue_path)).initialize()
        notification_store = ShipmentNotificationStore(
            self._path(settings.queue_path)
        )
        scan_owner = str(task_id or f"notification-scan-{uuid4().hex}")
        if not notification_store.try_acquire_scan_lock(scan_owner):
            report = {
                "eligible_order_count": 0,
                "new_draft_count": 0,
                "failed_order_count": 0,
                "scan_lock_busy_count": 1,
            }
            return {
                "status": "completed_with_warnings",
                "message": (
                    "已有客户通知扫描正在运行，本次未重复扫描；"
                    "未调用领星、Alibaba、ERP 写入、邮件或短信。"
                ),
                "notification_sync": report,
                "alibaba_logistics_query_count": 0,
                "external_provider_calls": 0,
                "erp_write_calls": 0,
            }
        recipient_name_resolver = configuration.get(
            "_runtime_notification_recipient_name_resolver"
        )
        if not callable(recipient_name_resolver):
            recipient_name_resolver = None
        progress_callback = configuration.get(
            "_runtime_notification_sync_progress"
        )
        if not callable(progress_callback):
            progress_callback = None
        client = None
        try:
            gateway, client = await self.create_gateway(settings)
            report = await sync_notification_drafts(
                gateway,
                notification_store,
                NotificationConfiguration.from_mapping(configuration),
                contact_backfill=lambda targets: self._backfill_notification_contacts(
                    settings,
                    notification_store,
                    targets,
                ),
                platform_order_nos=platform_order_nos,
                include_deferred_retries=bool(
                    configuration.get(
                        NOTIFICATION_SYNC_INCLUDE_DEFERRED_RETRIES_KEY,
                        False,
                    )
                ),
                recipient_name_resolver=recipient_name_resolver,
                progress_callback=progress_callback,
                discovery_filter_windows=(
                    self._notification_order_filters()
                    if platform_order_nos is None
                    else None
                ),
            )
        finally:
            if client is not None:
                await client.aclose()
            notification_store.release_scan_lock(scan_owner)
        failed_count = int(report.get("failed_order_count") or 0)
        warning_count = (
            failed_count
            + int(report.get("discovery_error_count") or 0)
            + int(report.get("scan_lock_busy_count") or 0)
        )
        return {
            "status": "completed_with_warnings" if warning_count else "completed",
            "message": (
                f"{self._notification_sync_summary_text(report)}"
                "本次仅同步领星订单与 WMS 物流，未发送邮件或短信。"
            ),
            "notification_sync": dict(report),
            "alibaba_logistics_query_count": 0,
            "external_provider_calls": 0,
            "erp_write_calls": 0,
        }

    async def diagnose_shipment_notification_outbound(
        self,
        settings: DesktopSettings,
        platform_order_no: str,
    ) -> Mapping[str, Any]:
        """Query one order through the server's Lingxing client without writes."""

        from shipment_automation.notification_sync import (
            diagnose_notification_outbound,
        )

        client = None
        try:
            gateway, client = await self.create_gateway(settings)
            return await diagnose_notification_outbound(
                gateway,
                platform_order_no,
            )
        finally:
            if client is not None:
                await client.aclose()

    @staticmethod
    async def _drain_shipment_product_identity_backfill(
        gateway: LingxingGateway,
        queue: ShipmentQueueStore,
        *,
        run_id: str,
        batch_size: int = _PRODUCT_IDENTITY_BACKFILL_BATCH_SIZE,
        target_budget: int = _PRODUCT_IDENTITY_BACKFILL_TARGET_BUDGET,
    ) -> tuple[dict[str, Any], tuple[str, ...], bool]:
        """Drain due historical identities in fair, bounded batches.

        Failed identities are persisted with a retry deadline by the queue
        store, so a transiently failing old order cannot consume the first
        slots forever.  The target budget keeps one scan bounded while normal
        backlogs can be drained through consecutive batches.
        """

        metrics: dict[str, Any] = {
            "target_count": 0,
            "checked_job_count": 0,
            "resolved_job_count": 0,
            "unresolved_job_count": 0,
            "failed_target_count": 0,
            "retry_scheduled_job_count": 0,
            "sku_target_count": 0,
            "sku_resolved_job_count": 0,
            "batch_count": 0,
            "remaining_target_count": 0,
            "remaining_due_target_count": 0,
            "deferred_target_count": 0,
            "target_budget_exhausted": False,
        }
        request_ids: list[str] = []
        runtime_failed = False
        bounded_batch_size = max(1, min(int(batch_size or 1), 100))
        bounded_target_budget = max(
            bounded_batch_size,
            min(int(target_budget or bounded_batch_size), 2000),
        )

        try:
            sku_targets = queue.list_completed_sku_product_identity_jobs(
                limit=bounded_target_budget,
            )
            if sku_targets:
                sku_metrics = queue.apply_product_identity_backfill(
                    (
                        {
                            "system_order_no": target["system_order_no"],
                            "platform_order_no": target["platform_order_no"],
                            "product_types": target["product_types"],
                            "observed_skus": target["sku_text"],
                            "evidence_scope": "completed_exact_sku",
                            "evidence_system_order_nos": (
                                target["system_order_no"],
                            ),
                            "completed_only": True,
                        }
                        for target in sku_targets
                    ),
                    catalog_version=PRODUCT_IDENTITY_CATALOG_VERSION,
                    run_id=run_id,
                )
                metrics["batch_count"] = 1
                metrics["sku_target_count"] = int(
                    sku_metrics.get("target_count") or 0
                )
                metrics["sku_resolved_job_count"] = int(
                    sku_metrics.get("resolved_job_count") or 0
                )
                for key in (
                    "target_count",
                    "checked_job_count",
                    "resolved_job_count",
                    "unresolved_job_count",
                    "failed_target_count",
                    "retry_scheduled_job_count",
                ):
                    metrics[key] = int(metrics[key]) + int(
                        sku_metrics.get(key) or 0
                    )
        except Exception:
            runtime_failed = True

        while (
            not runtime_failed
            and int(metrics["target_count"]) < bounded_target_budget
        ):
            remaining_budget = bounded_target_budget - int(metrics["target_count"])
            try:
                targets = queue.list_missing_product_type_jobs(
                    catalog_version=PRODUCT_IDENTITY_CATALOG_VERSION,
                    limit=min(bounded_batch_size, remaining_budget),
                )
                if not targets:
                    break
                observations, batch_request_ids = (
                    await read_order_product_type_details(gateway, targets)
                )
                batch_metrics = queue.apply_product_identity_backfill(
                    observations,
                    catalog_version=PRODUCT_IDENTITY_CATALOG_VERSION,
                    run_id=run_id,
                )
            except Exception:
                runtime_failed = True
                break

            batch_target_count = int(batch_metrics.get("target_count") or 0)
            if batch_target_count <= 0:
                runtime_failed = True
                break
            metrics["batch_count"] = int(metrics["batch_count"]) + 1
            for key in (
                "target_count",
                "checked_job_count",
                "resolved_job_count",
                "unresolved_job_count",
                "failed_target_count",
                "retry_scheduled_job_count",
            ):
                metrics[key] = int(metrics[key]) + int(batch_metrics.get(key) or 0)
            request_ids.extend(batch_request_ids)

        try:
            remaining = queue.product_identity_backfill_counts(
                catalog_version=PRODUCT_IDENTITY_CATALOG_VERSION
            )
        except Exception:
            runtime_failed = True
            remaining = {
                "total_target_count": 0,
                "due_target_count": 0,
                "deferred_target_count": 0,
            }
        metrics["remaining_target_count"] = int(
            remaining.get("total_target_count") or 0
        )
        metrics["remaining_due_target_count"] = int(
            remaining.get("due_target_count") or 0
        )
        metrics["deferred_target_count"] = int(
            remaining.get("deferred_target_count") or 0
        )
        metrics["target_budget_exhausted"] = bool(
            int(metrics["target_count"]) >= bounded_target_budget
            and int(metrics["remaining_due_target_count"]) > 0
        )
        return (
            metrics,
            tuple(dict.fromkeys(request_ids)),
            runtime_failed,
        )

    async def scan_shipments(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        task_id: str | None = None,
        operator_name: str = "",
        operator_email: str = "",
    ) -> Mapping[str, Any]:
        audit_task_id = self._scan_task_id(task_id, scan_kind="shipment")
        started_at = datetime.now(timezone.utc)
        filter_windows = self._shipment_order_filters()
        query = self._shipment_query_summary(filter_windows)
        queue: ShipmentQueueStore | None = None
        result: ShipmentApiScanResult | None = None
        queue_total_count: int | None = None
        email_preview_backfill_count = 0
        receiver_email_backfill_count = 0
        receiver_email_unresolved_count = 0
        product_type_backfill = {
            "target_count": 0,
            "checked_job_count": 0,
            "resolved_job_count": 0,
            "unresolved_job_count": 0,
            "failed_target_count": 0,
            "retry_scheduled_job_count": 0,
            "sku_target_count": 0,
            "sku_resolved_job_count": 0,
            "batch_count": 0,
            "remaining_target_count": 0,
            "remaining_due_target_count": 0,
            "deferred_target_count": 0,
            "target_budget_exhausted": False,
        }
        product_type_backfill_request_ids: tuple[str, ...] = ()
        product_type_backfill_runtime_failed = False
        email_preview_is_enabled = email_preview_enabled(configuration)
        scan_error: Exception | None = None
        try:
            queue = ShipmentQueueStore(self._path(settings.queue_path))
            gateway, client = await self.create_gateway(settings)
            try:
                result = await scan_shipment_candidates(
                    gateway,
                    queue,
                    settings.shipment_tag_name,
                    filter_windows=filter_windows,
                    dry_run=False,
                    # This is the current pending-review table range, not the
                    # lifetime order universe.  Absence from it never proves
                    # that an older queued order was completed.
                    reconcile_missing=False,
                )
                if result.complete:
                    (
                        product_type_backfill,
                        product_type_backfill_request_ids,
                        product_type_backfill_runtime_failed,
                    ) = await self._drain_shipment_product_identity_backfill(
                        gateway,
                        queue,
                        run_id=audit_task_id,
                    )
                if result.complete and email_preview_is_enabled:
                    # Older builds omitted buyer_email when persisting shipment
                    # candidates.  Repair only the completed email-error rows
                    # using a read-only order-detail call.  The raw address is
                    # never copied into scan diagnostics.
                    for target in queue.missing_receiver_email_targets():
                        try:
                            detail = await gateway.get_order_detail(
                                target["system_order_no"]
                            )
                            receiver_email = receiver_email_from_payload(detail.payload)
                            if receiver_email and queue.backfill_receiver_email(
                                system_order_no=target["system_order_no"],
                                platform_order_no=target["platform_order_no"],
                                receiver_email=receiver_email,
                                run_id=audit_task_id,
                            ):
                                receiver_email_backfill_count += 1
                            else:
                                receiver_email_unresolved_count += 1
                        except Exception:
                            receiver_email_unresolved_count += 1
            finally:
                await client.aclose()
            if result.complete and email_preview_is_enabled:
                # Local preview generation is intentionally independent from
                # the 30-day query range and from the customization payment
                # window.  The store keeps this operation idempotent and never
                # sends real email.
                email_preview_backfill_count = queue.prepare_email_batches_with_count()
        except Exception as error:
            scan_error = error

        # Alibaba is a browser-only source.  The server must never open it in a
        # headless browser because login challenges need an operator-visible
        # local Chrome.  The submitting desktop follows this API scan with a
        # separate ALIBABA_LOGISTICS task after the scan reaches a terminal
        # state.  If that desktop disconnects, queue rows remain pending until
        # an online client explicitly resumes the local browser query.
        # Customer-notification compensation is queued as a separate follow-up
        # task by the submitting desktop.  Keeping it outside this critical path
        # lets candidate rows become visible as soon as the scan itself ends.

        if queue is None:
            try:
                queue = ShipmentQueueStore(self._path(settings.queue_path))
            except Exception:
                queue = None
        if queue is not None:
            try:
                queue_total_count = len(queue.list_all_jobs())
            except Exception:
                queue_total_count = None

        diagnostic_codes: list[str] = []
        if scan_error is not None:
            diagnostic_codes.append("lingxing_scan_runtime_failure")
        if product_type_backfill_runtime_failed:
            diagnostic_codes.append("shipment_product_identity_backfill_failed")
        if int(product_type_backfill.get("failed_target_count") or 0):
            diagnostic_codes.append("shipment_product_identity_backfill_incomplete")
        if int(product_type_backfill.get("deferred_target_count") or 0):
            diagnostic_codes.append("shipment_product_identity_backfill_deferred")
        if bool(product_type_backfill.get("target_budget_exhausted")):
            diagnostic_codes.append("shipment_product_identity_backlog_remaining")

        payload = self._shipment_payload_metrics(
            result,
            queue_total_count=queue_total_count,
            query=query,
            email_preview_backfill_count=email_preview_backfill_count,
            receiver_email_backfill_count=receiver_email_backfill_count,
            receiver_email_unresolved_count=receiver_email_unresolved_count,
            product_type_backfill=product_type_backfill,
            product_type_backfill_request_ids=product_type_backfill_request_ids,
            extra_diagnostic_codes=tuple(diagnostic_codes),
        )
        if result is None:
            status = "failed"
        elif result is not None and result.state is not ApiScanState.COMPLETE:
            status = self._task_status(result.state)
        elif (
            scan_error is not None
            or product_type_backfill_runtime_failed
            or int(product_type_backfill.get("failed_target_count") or 0)
            or int(product_type_backfill.get("deferred_target_count") or 0)
            or bool(product_type_backfill.get("target_budget_exhausted"))
        ):
            status = "completed_with_warnings"
        else:
            status = "completed"
        scan_message = self._shipment_scan_message(
            result,
            queue_total_count,
            query=query,
            email_preview_backfill_count=email_preview_backfill_count,
            receiver_email_backfill_count=receiver_email_backfill_count,
            receiver_email_unresolved_count=receiver_email_unresolved_count,
            product_type_backfill=product_type_backfill,
            product_type_backfill_runtime_failed=product_type_backfill_runtime_failed,
            scan_error=scan_error,
        )
        payload.update({
            "status": status,
            "message": (
                f"{scan_message}；客户通知历史补偿将在独立后台任务中增量执行。"
            ),
            "alibaba_logistics_execution": "client_visible_browser_required",
            "alibaba_logistics_followup_pending": True,
            "notification_compensation_followup_pending": True,
        })
        return self._complete_scan_payload(
            settings=settings,
            task_id=audit_task_id,
            scan_kind="shipment",
            started_at=started_at,
            operator_name=operator_name,
            operator_email=operator_email,
            query=query,
            pages=result.pagination.page_traces if result is not None else (),
            order_decisions=(
                *(self._shipment_audit_decisions(result) if result is not None else ()),
            ),
            summary={
                **self._shipment_audit_summary(
                    result,
                    status=payload["status"],
                    queue_total_count=queue_total_count,
                    query=query,
                    email_preview_backfill_count=email_preview_backfill_count,
                    receiver_email_backfill_count=receiver_email_backfill_count,
                    receiver_email_unresolved_count=receiver_email_unresolved_count,
                    product_type_backfill=product_type_backfill,
                    diagnostic_codes=tuple(diagnostic_codes),
                ),
                "notification_compensation_followup_pending": True,
            },
            payload=payload,
            error=scan_error,
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
        operator_name: str,
        operator_email: str,
        query: Mapping[str, Any],
        pages: Any,
        order_decisions: Any,
        summary: Mapping[str, Any],
        payload: Mapping[str, Any],
        error: BaseException | None = None,
    ) -> Mapping[str, Any]:
        try:
            audit = self._audit_writer(settings).write(
                task_id=task_id,
                scan_kind=scan_kind,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                operator_name=operator_name,
                operator_email=operator_email,
                query=query,
                pages=pages,
                order_decisions=order_decisions,
                summary=summary,
                error=error,
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
        operator_name: str,
        operator_email: str,
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
                operator_name=operator_name,
                operator_email=operator_email,
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
    def _shipment_logistics_audit_decisions(
        report: Mapping[str, Any] | None,
    ) -> tuple[Mapping[str, Any], ...]:
        if not report:
            return ()
        decisions: list[Mapping[str, Any]] = []
        for raw in report.get("query_results") or ():
            item = raw if isinstance(raw, Mapping) else {}
            detail_value = item.get("detail")
            detail = detail_value if isinstance(detail_value, Mapping) else {}
            state = str(item.get("logistics_state") or "UNKNOWN").strip().upper()
            decisions.append(
                {
                    "platform_order_no": item.get("platform_order_no"),
                    "system_order_no": item.get("system_order_no"),
                    "logistics_no": item.get("logistics_no"),
                    "international_tracking_no": detail.get(
                        "international_tracking_no"
                    ),
                    "carrier": detail.get("carrier"),
                    "alibaba_status": item.get("status_text")
                    or detail.get("status_text"),
                    "logistics_state": state,
                    "decision": f"logistics_{state.casefold()}",
                    "reason_code": f"logistics_{state.casefold()}",
                    "reason": item.get("last_error") or "",
                }
            )
        return tuple(decisions)

    @staticmethod
    def _shipment_logistics_metrics(
        report: Mapping[str, Any] | None,
    ) -> dict[str, int]:
        results = list(report.get("query_results") or ()) if report else []
        counts = {"READY": 0, "WAITING": 0, "BLOCKED": 0, "RETRYABLE": 0}
        for raw in results:
            item = raw if isinstance(raw, Mapping) else {}
            state = str(item.get("logistics_state") or "").strip().upper()
            if state in counts:
                counts[state] += 1
        return {
            "logistics_query_count": len(results),
            "logistics_parsed_count": int((report or {}).get("parsed_count") or 0),
            "logistics_ready_count": counts["READY"],
            "logistics_waiting_count": counts["WAITING"],
            "logistics_blocked_count": counts["BLOCKED"],
            "logistics_retryable_count": counts["RETRYABLE"],
            "ready_to_mark_count": int((report or {}).get("ready_count") or 0),
        }

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
                    "product_identity_pending_count": (
                        result.product_identity_pending_count
                    ),
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
        receiver_email_backfill_count: int = 0,
        receiver_email_unresolved_count: int = 0,
        product_type_backfill: Mapping[str, Any] | None = None,
        logistics_report: Mapping[str, Any] | None = None,
        diagnostic_codes: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        all_diagnostic_codes = list(
            dict.fromkeys(
                [
                    *(
                        item.code
                        for item in (result.diagnostics if result is not None else ())
                    ),
                    *diagnostic_codes,
                ]
            )
        )
        summary: dict[str, Any] = {
            "status": status,
            "scan_start_time": int(query["start_time"]),
            "scan_end_time": int(query["end_time"]),
            "window_count": int(query.get("window_count") or 0),
            "email_preview_backfill_count": int(email_preview_backfill_count),
            "receiver_email_backfill_count": int(receiver_email_backfill_count),
            "receiver_email_unresolved_count": int(receiver_email_unresolved_count),
            "product_type_backfill_target_count": int(
                (product_type_backfill or {}).get("target_count") or 0
            ),
            "product_type_backfill_checked_job_count": int(
                (product_type_backfill or {}).get("checked_job_count") or 0
            ),
            "product_type_backfill_resolved_job_count": int(
                (product_type_backfill or {}).get("resolved_job_count") or 0
            ),
            "product_type_backfill_sku_target_count": int(
                (product_type_backfill or {}).get("sku_target_count") or 0
            ),
            "product_type_backfill_sku_resolved_job_count": int(
                (product_type_backfill or {}).get("sku_resolved_job_count") or 0
            ),
            "product_type_backfill_unresolved_job_count": int(
                (product_type_backfill or {}).get("unresolved_job_count") or 0
            ),
            "product_type_backfill_failed_target_count": int(
                (product_type_backfill or {}).get("failed_target_count") or 0
            ),
            "product_type_backfill_retry_scheduled_job_count": int(
                (product_type_backfill or {}).get("retry_scheduled_job_count") or 0
            ),
            "product_type_backfill_batch_count": int(
                (product_type_backfill or {}).get("batch_count") or 0
            ),
            "product_type_backfill_remaining_target_count": int(
                (product_type_backfill or {}).get("remaining_target_count") or 0
            ),
            "product_type_backfill_remaining_due_target_count": int(
                (product_type_backfill or {}).get("remaining_due_target_count") or 0
            ),
            "product_type_backfill_deferred_target_count": int(
                (product_type_backfill or {}).get("deferred_target_count") or 0
            ),
            "product_type_backfill_target_budget_exhausted": bool(
                (product_type_backfill or {}).get("target_budget_exhausted")
            ),
            "product_identity_catalog_version": PRODUCT_IDENTITY_CATALOG_VERSION,
            "alibaba_logistics_execution": "client_visible_browser_required",
            **DesktopApiServices._shipment_logistics_metrics(logistics_report),
        }
        if all_diagnostic_codes:
            summary["diagnostic_codes"] = all_diagnostic_codes
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
                    "diagnostic_codes": all_diagnostic_codes,
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
        receiver_email_backfill_count: int = 0,
        receiver_email_unresolved_count: int = 0,
        product_type_backfill: Mapping[str, Any] | None = None,
        product_type_backfill_request_ids: tuple[str, ...] = (),
        logistics_report: Mapping[str, Any] | None = None,
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
            "receiver_email_backfill_count": int(receiver_email_backfill_count),
            "receiver_email_unresolved_count": int(receiver_email_unresolved_count),
            "product_type_backfill_target_count": int(
                (product_type_backfill or {}).get("target_count") or 0
            ),
            "product_type_backfill_checked_job_count": int(
                (product_type_backfill or {}).get("checked_job_count") or 0
            ),
            "product_type_backfill_resolved_job_count": int(
                (product_type_backfill or {}).get("resolved_job_count") or 0
            ),
            "product_type_backfill_sku_target_count": int(
                (product_type_backfill or {}).get("sku_target_count") or 0
            ),
            "product_type_backfill_sku_resolved_job_count": int(
                (product_type_backfill or {}).get("sku_resolved_job_count") or 0
            ),
            "product_type_backfill_unresolved_job_count": int(
                (product_type_backfill or {}).get("unresolved_job_count") or 0
            ),
            "product_type_backfill_failed_target_count": int(
                (product_type_backfill or {}).get("failed_target_count") or 0
            ),
            "product_type_backfill_retry_scheduled_job_count": int(
                (product_type_backfill or {}).get("retry_scheduled_job_count") or 0
            ),
            "product_type_backfill_batch_count": int(
                (product_type_backfill or {}).get("batch_count") or 0
            ),
            "product_type_backfill_remaining_target_count": int(
                (product_type_backfill or {}).get("remaining_target_count") or 0
            ),
            "product_type_backfill_remaining_due_target_count": int(
                (product_type_backfill or {}).get("remaining_due_target_count") or 0
            ),
            "product_type_backfill_deferred_target_count": int(
                (product_type_backfill or {}).get("deferred_target_count") or 0
            ),
            "product_type_backfill_target_budget_exhausted": bool(
                (product_type_backfill or {}).get("target_budget_exhausted")
            ),
            "product_identity_catalog_version": PRODUCT_IDENTITY_CATALOG_VERSION,
            "window_count": int(query.get("window_count") or 0),
            "scan_start_time": int(query["start_time"]),
            "scan_end_time": int(query["end_time"]),
            "queue_total_count": queue_total_count,
            "request_ids": [],
            "diagnostic_codes": list(dict.fromkeys(diagnostic_codes)),
            **DesktopApiServices._shipment_logistics_metrics(logistics_report),
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
                "request_ids": list(
                    dict.fromkeys(
                        (
                            *result.pagination.request_ids,
                            *product_type_backfill_request_ids,
                        )
                    )
                ),
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

    @classmethod
    def _notification_order_filters(
        cls,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Return the same 30-day windows without the auto-mark status filter."""

        return tuple(
            {
                key: value
                for key, value in window.items()
                if key != "order_status"
            }
            for window in cls._shipment_order_filters(now)
        )

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
    def _parse_workflow_scan_time(value: object) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _workflow_folder_search_months(
        cls,
        workflow: Mapping[str, Any],
        *,
        payment_window_hours: int,
    ) -> tuple[tuple[int, int], ...]:
        """Return only months that can contain a scanned candidate's folder."""

        anchor = cls._parse_workflow_scan_time(workflow.get("last_seen_at"))
        if anchor is None:
            anchor = cls._parse_workflow_scan_time(workflow.get("created_at"))
        if anchor is None:
            return ()
        # Candidate payment time is at most the configured window before the
        # first scan.  One day on either side covers UTC/China date boundaries.
        start = anchor - timedelta(hours=max(1, int(payment_window_hours)), days=1)
        end = anchor + timedelta(days=1)
        cursor = date(start.year, start.month, 1)
        last = date(end.year, end.month, 1)
        months: list[tuple[int, int]] = []
        while cursor <= last:
            months.append((cursor.year, cursor.month))
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)
        return tuple(months)

    def _find_missing_candidate_order_folders(
        self,
        folder_root: str | Path,
        workflows: list[Mapping[str, Any]],
        *,
        payment_window_hours: int,
    ) -> tuple[dict[str, bool], tuple[str, ...]]:
        """Index relevant month directories once and match platform order IDs.

        Any filesystem traversal error aborts the lookup before database
        mutation.  This prevents an unavailable network drive from being
        interpreted as proof that every order folder is absent.
        """

        root = self._path(folder_root)
        root_stat = root.stat()
        if not stat.S_ISDIR(root_stat.st_mode):
            raise NotADirectoryError(str(root))

        workflow_by_order = {
            str(item.get("platform_order_no") or "").strip(): item
            for item in workflows
            if str(item.get("platform_order_no") or "").strip()
        }
        order_months: dict[str, tuple[tuple[int, int], ...]] = {}
        unresolved: list[str] = []
        for order_no, workflow in workflow_by_order.items():
            months = self._workflow_folder_search_months(
                workflow,
                payment_window_hours=payment_window_hours,
            )
            if not months:
                unresolved.append(order_no)
                continue
            order_months[order_no] = months

        outcomes = {order_no: False for order_no in order_months}
        orders_by_month: dict[tuple[int, int], set[str]] = {}
        for order_no, months in order_months.items():
            for month in months:
                orders_by_month.setdefault(month, set()).add(order_no)

        for (year, month), month_orders in sorted(orders_by_month.items()):
            targets = {order_no for order_no in month_orders if not outcomes[order_no]}
            if not targets:
                continue
            matched = find_platform_order_folders(
                root,
                date(year, month, 1),
                targets,
                strict=True,
            )
            for order_no in matched:
                outcomes[order_no] = True
        return outcomes, tuple(unresolved)

    @staticmethod
    def _persist_custom_candidates(
        store: CustomWorkflowStore,
        result: CustomizationApiScanResult,
    ) -> None:
        seen_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        # A complete scan may observe an order that is already present in the
        # workflow store (including a completed historical order).  Fill only
        # identity metadata that the legacy migration did not carry across;
        # the store method deliberately leaves every workflow/stage state
        # untouched and never overwrites a value that is already present.
        for observation in result.observed_workflows:
            store.backfill_workflow_identity(
                str(observation.get("platform_order_no") or ""),
                system_order_no=str(observation.get("system_order_no") or ""),
                product_type=str(observation.get("product_type") or ""),
                product_types=tuple(observation.get("product_types") or ()),
                actor="api_scanner",
            )
        for observation in result.product_identity_observations:
            existing = store.get_legacy_record(observation.platform_order_no)
            existing_identity_state = str(
                (existing or {}).get("product_identity_state") or ""
            ).strip()
            if existing is not None and existing_identity_state not in (
                _PRODUCT_IDENTITY_STATES
            ):
                # Never demote an already actionable workflow merely because
                # one list response temporarily omitted its ASIN.
                continue

            def retain_identity(
                current: dict[str, Any],
                *,
                item=observation,
            ) -> dict[str, Any]:
                record = dict(current)
                captured_at = str(
                    record.get("product_identity_captured_at") or seen_at
                )
                try:
                    attempt_count = int(
                        record.get("product_identity_detail_attempt_count") or 0
                    )
                except (TypeError, ValueError):
                    attempt_count = 0
                if item.detail_attempted:
                    attempt_count += 1
                record.update(
                    {
                        "platform_order_no": item.platform_order_no,
                        "system_order_no": item.system_order_no,
                        "workflow_status": item.state,
                        "last_seen_at": record.get("last_seen_at") or seen_at,
                        "product_identity_state": item.state,
                        "product_identity_status_text": item.status_text,
                        "product_identity_last_error": item.last_error,
                        "product_identity_last_checked_at": seen_at,
                        "product_identity_captured_at": captured_at,
                        "product_identity_detail_attempt_count": attempt_count,
                        "product_identity_sku": item.sku,
                        "product_identity_paid_at": item.paid_at_text,
                        "product_identity_tag_text": item.tag_text,
                        "product_identity_observed_asins": list(
                            item.observed_asins
                        ),
                        **(
                            {
                                "product_types": list(item.product_types),
                                "product_type": " | ".join(item.product_types),
                            }
                            if item.product_types
                            else {}
                        ),
                    }
                )
                return record

            store.mutate_legacy_record(
                observation.platform_order_no,
                retain_identity,
                event_type=(
                    "api_product_identity_retained"
                    if existing is not None
                    else "api_product_identity_captured"
                ),
                actor="api_scanner",
                reason=observation.status_text,
            )
        for candidate in result.candidates:
            candidate_metadata = {
                "api_candidate_paid_at": candidate.paid_at_text,
                "api_candidate_asin": candidate.asin,
                "api_candidate_sku": candidate.sku,
                "api_candidate_parent_asin": candidate.parent_asin,
                "api_candidate_product_type": candidate.product_type,
                "api_candidate_product_types": list(candidate.product_types),
                "api_candidate_logistics": candidate.logistics,
                "api_candidate_sales_revenue_total": candidate.sales_revenue_total,
                "api_candidate_sales_revenue_currency": candidate.sales_revenue_currency,
                "api_candidate_sales_revenue_status": candidate.sales_revenue_status,
                "api_candidate_sales_revenue_source": candidate.sales_revenue_source,
                "api_candidate_captured_at": seen_at,
            }
            existing = store.get_legacy_record(candidate.platform_order_no)
            existing_identity_state = str(
                (existing or {}).get("product_identity_state") or ""
            ).strip()
            if existing is not None and existing_identity_state in (
                _PRODUCT_IDENTITY_STATES
            ):

                def resolve_identity(
                    current: dict[str, Any],
                    *,
                    item=candidate,
                ) -> dict[str, Any]:
                    record = dict(current)
                    for key in _PRODUCT_IDENTITY_RECORD_KEYS:
                        record.pop(key, None)
                    record.update(
                        {
                            "platform_order_no": item.platform_order_no,
                            "system_order_no": item.system_order_no,
                            "product_type": item.product_type,
                            "product_types": list(item.product_types),
                            "workflow_status": "pending",
                            "last_seen_at": record.get("last_seen_at") or seen_at,
                            **candidate_metadata,
                        }
                    )
                    return record

                store.mutate_legacy_record(
                    candidate.platform_order_no,
                    resolve_identity,
                    event_type="api_product_identity_resolved",
                    actor="api_scanner",
                    reason="ASIN 已同步并匹配到受支持的定制产品。",
                )
                continue

            # Never rewrite an existing ordinary workflow during a scan.  A
            # targeted API observation refresh is safe; recreating stages is not.
            if existing is not None:
                store.backfill_workflow_identity(
                    candidate.platform_order_no,
                    system_order_no=candidate.system_order_no,
                    product_type=candidate.product_type,
                    product_types=candidate.product_types,
                    actor="api_scanner",
                )
                store.mutate_legacy_record(
                    candidate.platform_order_no,
                    lambda current, metadata=candidate_metadata: {
                        **current,
                        **metadata,
                    },
                    event_type="api_candidate_metadata_refreshed",
                    actor="api_scanner",
                )
                continue

            def initial_record(_old: dict[str, Any], *, item=candidate) -> dict[str, Any]:
                return {
                    "platform_order_no": item.platform_order_no,
                    "system_order_no": item.system_order_no,
                    "product_type": item.product_type,
                    "product_types": list(item.product_types),
                    "workflow_status": "pending",
                    "last_seen_at": seen_at,
                    **candidate_metadata,
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

    def _backfill_notification_contacts(
        self,
        settings: DesktopSettings,
        notification_store: Any,
        targets: list[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        from erp_automation.application.notification_contact_backfill import (
            backfill_missing_notification_contacts,
        )

        return backfill_missing_notification_contacts(
            targets,
            notification_store=notification_store,
            workflow_store=CustomWorkflowStore(
                self._path(settings.custom_state_path)
            ),
            folder_root=self._path(settings.folder_root),
            staging_root=self.workspace / "logs" / "custom_zip_staging",
        )

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
            f"候选 {result.candidate_count} 个，商品信息待同步/复核 "
            f"{result.product_identity_pending_count} 个；跳过统计：{skip_text}。"
        )
        if result.state is ApiScanState.COMPLETE:
            return f"定制订单 API 扫描完成：{metrics}"
        return (
            "定制订单 API 快照不完整；未更新候选数据库，也未返回可操作订单。"
            f"{metrics}"
        )

    @staticmethod
    def _shipment_scan_message(
        result: ShipmentApiScanResult | None,
        queue_total_count: int | None,
        *,
        query: Mapping[str, Any],
        email_preview_backfill_count: int,
        receiver_email_backfill_count: int = 0,
        receiver_email_unresolved_count: int = 0,
        product_type_backfill: Mapping[str, Any] | None = None,
        product_type_backfill_runtime_failed: bool = False,
        scan_error: Exception | None = None,
    ) -> str:
        queue_text = str(queue_total_count) if queue_total_count is not None else "读取失败"
        lingxing_text = (
            "领星阶段失败"
            if scan_error is not None
            else (
                f"领星读取 {result.api_raw_order_count} 个、候选 {result.candidate_count} 个、"
                f"新增 {result.enqueued_count} 个"
                if result is not None
                else "领星阶段未完成"
            )
        )
        message = (
            f"自动标发领星扫描完成：{lingxing_text}；当前队列共 {queue_text} 个。"
            "服务器只更新共享队列，不读取阿里物流网页；"
            "物流记录等待本机 Chrome 查询，并将由发起扫描的在线客户端"
            "使用本机可见 Chrome 继续处理。"
        )
        if result is not None and result.enqueued_count == 0:
            message += " 本次新增为 0 只表示没有新任务，不代表当前队列为空。"
        if result is not None and result.state is ApiScanState.INCOMPLETE:
            message = (
                "领星待审核快照不完整，未写入不完整快照中的候选；"
                "历史到期物流记录仍保留，等待本机 Chrome 查询。"
                + message
            )
        elif scan_error is not None:
            message = "本轮部分完成；失败阶段可在详细扫描日志中检查。" + message
        if email_preview_backfill_count:
            message += f" 本地邮件预览补建或更新 {email_preview_backfill_count} 个。"
        if receiver_email_backfill_count:
            message += f" 已从订单详情安全补齐历史收件邮箱 {receiver_email_backfill_count} 个。"
        if receiver_email_unresolved_count:
            message += (
                f" 仍有 {receiver_email_unresolved_count} 个历史收件邮箱未能读取，"
                "请在详细扫描日志中检查。"
            )
        resolved_product_jobs = int(
            (product_type_backfill or {}).get("resolved_job_count") or 0
        )
        sku_resolved_product_jobs = int(
            (product_type_backfill or {}).get("sku_resolved_job_count") or 0
        )
        asin_resolved_product_jobs = max(
            0,
            resolved_product_jobs - sku_resolved_product_jobs,
        )
        unresolved_product_jobs = int(
            (product_type_backfill or {}).get("unresolved_job_count") or 0
        )
        failed_product_targets = int(
            (product_type_backfill or {}).get("failed_target_count") or 0
        )
        product_backfill_batches = int(
            (product_type_backfill or {}).get("batch_count") or 0
        )
        remaining_due_product_targets = int(
            (product_type_backfill or {}).get("remaining_due_target_count") or 0
        )
        deferred_product_targets = int(
            (product_type_backfill or {}).get("deferred_target_count") or 0
        )
        if product_backfill_batches:
            message += f" 历史商品类型已连续处理 {product_backfill_batches} 批。"
        if sku_resolved_product_jobs:
            message += (
                f" 已按完整订单中的精确 SKU 补齐 {sku_resolved_product_jobs} 个"
                "已完成历史商品类型。"
            )
        if asin_resolved_product_jobs:
            message += (
                f" 已按订单明细中的 ASIN 补齐 {asin_resolved_product_jobs} 个"
                "历史商品类型。"
            )
        if unresolved_product_jobs:
            message += (
                f" 已核验 {unresolved_product_jobs} 个历史订单，但目录暂未识别其 ASIN；"
                "目录更新后会自动复核。"
            )
        if failed_product_targets:
            message += (
                f" 有 {failed_product_targets} 个历史商品类型详情读取未完成，"
                "已延后重试且不会阻塞后续订单。"
            )
        elif product_type_backfill_runtime_failed:
            message += " 历史商品类型回填过程未完成，后续扫描会安全重试。"
        if remaining_due_product_targets:
            message += (
                f" 本轮限额后仍有 {remaining_due_product_targets} 个可立即处理的历史订单，"
                "后续扫描将继续排空。"
            )
        if deferred_product_targets:
            message += (
                f" 另有 {deferred_product_targets} 个读取失败订单正在退避等待重试。"
            )
        return message


__all__ = ["DesktopApiServices", "build_capability_router"]
