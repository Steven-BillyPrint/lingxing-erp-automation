"""In-process business task runner used by the desktop application."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, is_dataclass
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from erp_automation.ui.models import (
    Capability,
    DesktopSettings,
    DesktopInteractionOption,
    DesktopInteractionResponse,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY,
    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
    TaskArea,
    TaskCommand,
)
from lingxing_automation.services.custom_order_api import CustomOrderApiOperations

from .email_policy import email_preview_enabled


ScanCallable = Callable[
    [DesktopSettings, Mapping[str, Any], str | None],
    Awaitable[Mapping[str, Any]],
]
NotificationSyncCallable = Callable[
    [DesktopSettings, Mapping[str, Any], str | None, tuple[str, ...] | None],
    Awaitable[Mapping[str, Any]],
]
NotificationSendCallable = Callable[
    [DesktopSettings, Mapping[str, Any], str | None, tuple[str, ...]],
    Awaitable[Mapping[str, Any]],
]
NotificationContactRefreshCallable = Callable[
    [DesktopSettings, Mapping[str, Any], str | None, tuple[int, ...]],
    Awaitable[Mapping[str, Any]],
]
ErpMarkCallable = Callable[..., Awaitable[str]]
CustomOrderOperationsFactory = Callable[
    [DesktopSettings, Mapping[str, Any]],
    AbstractAsyncContextManager[CustomOrderApiOperations],
]
CustomOrderStatusCheck = Callable[
    [DesktopSettings, Mapping[str, Any], str, str],
    Awaitable[Any],
]
RuntimeWriteGuardProvider = Callable[[], bool]
InteractionHandler = Callable[..., Awaitable[DesktopInteractionResponse]]
CancellationProvider = Callable[[str], bool]
ProgressHandler = Callable[[str, str, int], None]


class _ShutdownTaskCancelled(Exception):
    pass


@dataclass(frozen=True)
class TaskExecutionResult:
    succeeded: bool
    message: str
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    blocked: bool = False
    cancelled: bool = False


class DesktopTaskRunner:
    """Run existing business workflows inside one serial desktop worker.

    No BAT file, CLI subprocess, or legacy launcher is used.  The argparse
    parsers remain useful as a single source of safe defaults, while credentials
    are injected as an in-memory mapping from the DPAPI encrypted store.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        settings_provider: Callable[[], DesktopSettings],
        configuration_provider: Callable[[], Mapping[str, Any]],
        custom_scan: ScanCallable | None = None,
        shipment_scan: ScanCallable | None = None,
        shipment_notification_sync: NotificationSyncCallable | ScanCallable | None = None,
        shipment_notification_send: NotificationSendCallable | None = None,
        shipment_notification_contact_refresh: NotificationContactRefreshCallable | None = None,
        api_test: ScanCallable | None = None,
        erp_mark_func: ErpMarkCallable | None = None,
        custom_order_api_factory: CustomOrderOperationsFactory | None = None,
        custom_order_status_check: CustomOrderStatusCheck | None = None,
        runtime_write_guard_provider: RuntimeWriteGuardProvider | None = None,
        interaction_handler: InteractionHandler | None = None,
        cancellation_provider: CancellationProvider | None = None,
        progress_handler: ProgressHandler | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.settings_provider = settings_provider
        self.configuration_provider = configuration_provider
        self.custom_scan = custom_scan
        self.shipment_scan = shipment_scan
        self.shipment_notification_sync = shipment_notification_sync
        self.shipment_notification_send = shipment_notification_send
        self.shipment_notification_contact_refresh = shipment_notification_contact_refresh
        self.api_test = api_test
        self.erp_mark_func = erp_mark_func
        self.custom_order_api_factory = custom_order_api_factory
        self.custom_order_status_check = custom_order_status_check
        self.runtime_write_guard_provider = runtime_write_guard_provider
        self.interaction_handler = interaction_handler
        self.cancellation_provider = cancellation_provider
        self.progress_handler = progress_handler
        self._consumed_confirmation_ids: set[str] = set()

    def _report_progress(
        self,
        task_id: str,
        message: str,
        progress_percent: int,
    ) -> None:
        if self.progress_handler is None or not task_id:
            return
        try:
            self.progress_handler(
                task_id,
                message,
                max(0, min(99, int(progress_percent))),
            )
        except Exception:
            # Progress reporting is diagnostic and must never fail a workflow.
            pass

    def __call__(self, command: TaskCommand) -> TaskExecutionResult:
        return asyncio.run(self.run(command))

    async def run(self, command: TaskCommand) -> TaskExecutionResult:
        settings = self.settings_provider()
        configuration = dict(self.configuration_provider())
        if command.area is TaskArea.MAINTENANCE and command.capability is Capability.LIST_ORDERS:
            if self.api_test is None:
                return TaskExecutionResult(False, "领星 API 连接测试器尚未连接。")
            try:
                value = await self._await_cancellable(
                    self.api_test(settings, configuration, command.execution_id),
                    command.execution_id,
                )
            except _ShutdownTaskCancelled:
                return self._shutdown_cancelled_result()
            payload = dict(value)
            return self._result(payload, success_statuses={"completed"})
        if command.area is TaskArea.CUSTOMIZATION and command.capability is Capability.LIST_ORDERS:
            if self.custom_scan is None:
                return TaskExecutionResult(False, "API 定制订单扫描器尚未连接。")
            try:
                value = await self._await_cancellable(
                    self.custom_scan(settings, configuration, command.execution_id),
                    command.execution_id,
                )
            except _ShutdownTaskCancelled:
                return self._shutdown_cancelled_result()
            payload = dict(value)
            return self._result(payload, success_statuses={"completed"})
        if command.area is TaskArea.CUSTOMIZATION:
            if not command.order_no:
                return TaskExecutionResult(False, "处理定制订单必须先选择平台单号。")
            try:
                confirmation = self._consume_confirmation(
                    command,
                    DesktopWriteAction.PROCESS_CUSTOM_ORDER,
                    system_order_no=str(command.payload.get("system_order_no") or ""),
                )
            except ValueError as exc:
                return TaskExecutionResult(False, str(exc), blocked=True)
            return await self._process_custom_order(
                command.order_no,
                settings,
                configuration,
                confirmation,
                task_id=command.execution_id or "",
                browser_endpoint=str(
                    command.payload.get(DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY) or ""
                ),
            )
        if (
            command.area is TaskArea.SHIPMENT
            and command.capability is Capability.GET_ORDER_DETAIL
            and str(command.payload.get("trigger") or "")
            == NOTIFICATION_CONTACT_REFRESH_TRIGGER
        ):
            if self.shipment_notification_contact_refresh is None:
                return TaskExecutionResult(False, "定制 JSON 联系方式读取服务尚未连接。")
            notification_ids: list[int] = []
            for value in command.payload.get("notification_ids") or ():
                try:
                    notification_id = int(value)
                except (TypeError, ValueError):
                    continue
                if notification_id > 0 and notification_id not in notification_ids:
                    notification_ids.append(notification_id)
            if not notification_ids:
                return TaskExecutionResult(False, "请先选择至少一条客户通知。")
            sync_started = time.monotonic()
            self._report_progress(
                command.execution_id or "",
                "正在本机读取定制 JSON 联系方式并同步服务器通知队列。",
                35,
            )
            try:
                value = await self._await_cancellable(
                    self.shipment_notification_contact_refresh(
                        settings,
                        configuration,
                        command.execution_id,
                        tuple(notification_ids),
                    ),
                    command.execution_id,
                )
            except _ShutdownTaskCancelled:
                return self._shutdown_cancelled_result()
            payload = dict(value)
            payload["notification_contact_refresh_duration_ms"] = round(
                (time.monotonic() - sync_started) * 1000
            )
            return self._result(
                payload,
                success_statuses={"completed", "completed_with_warnings"},
            )
        if (
            command.area is TaskArea.SHIPMENT
            and command.capability is Capability.LIST_ORDERS
            and str(command.payload.get("trigger") or "")
            == NOTIFICATION_REVIEW_RESCAN_TRIGGER
        ):
            if self.shipment_notification_sync is None:
                return TaskExecutionResult(False, "客户通知物流同步器尚未连接。")
            sync_started = time.monotonic()
            self._report_progress(
                command.execution_id or "",
                "正在通过领星 API 同步客户通知物流状态。",
                40,
            )
            try:
                value = await self._await_cancellable(
                    self.shipment_notification_sync(
                        settings,
                        configuration,
                        command.execution_id,
                    ),
                    command.execution_id,
                )
            except _ShutdownTaskCancelled:
                return self._shutdown_cancelled_result()
            payload = dict(value)
            payload["notification_sync_duration_ms"] = round(
                (time.monotonic() - sync_started) * 1000
            )
            return self._result(payload, success_statuses={"completed"})
        if command.area is TaskArea.SHIPMENT and command.capability is Capability.LIST_ORDERS:
            if self.shipment_scan is None:
                return TaskExecutionResult(False, "API 自动标发扫描器尚未连接。")
            try:
                value = await self._await_cancellable(
                    self.shipment_scan(settings, configuration, command.execution_id),
                    command.execution_id,
                )
            except _ShutdownTaskCancelled:
                return self._shutdown_cancelled_result()
            payload = dict(value)
            return self._result(
                payload,
                success_statuses={"completed", "completed_with_warnings"},
            )
        if command.area is TaskArea.SHIPMENT and command.capability is Capability.ALIBABA_LOGISTICS:
            try:
                return await self._await_cancellable(
                    self._query_logistics(
                        settings,
                        configuration,
                        task_id=command.execution_id or "",
                        browser_endpoint=str(
                            command.payload.get(DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY) or ""
                        ),
                    ),
                    command.execution_id,
                )
            except _ShutdownTaskCancelled:
                return self._shutdown_cancelled_result()
        if command.area is TaskArea.SHIPMENT and command.capability is Capability.OUTBOUND_ORDER:
            logistics_no = str(command.payload.get("logistics_no") or "").strip()
            if not logistics_no:
                return TaskExecutionResult(False, "执行标发必须先选择有效物流单号。")
            try:
                confirmation = self._consume_confirmation(
                    command,
                    DesktopWriteAction.EXECUTE_ERP_MARK,
                    system_order_no=str(command.payload.get("system_order_no") or ""),
                    logistics_no=logistics_no,
                )
            except ValueError as exc:
                return TaskExecutionResult(False, str(exc), blocked=True)
            return await self._mark_shipment(
                logistics_no,
                settings,
                configuration,
                confirmation,
                task_id=command.execution_id or "",
                auto_send_customer_notification=bool(
                    command.payload.get("auto_send_customer_notification")
                ),
                browser_endpoint=str(
                    command.payload.get(DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY) or ""
                ),
            )
        return TaskExecutionResult(False, f"当前桌面任务没有执行器：{command.name}")

    async def _await_cancellable(
        self,
        awaitable: Awaitable[Any],
        task_id: str | None,
    ) -> Any:
        normalized_task_id = str(task_id or "").strip()
        if self.cancellation_provider is None or not normalized_task_id:
            return await awaitable
        task = asyncio.create_task(awaitable)
        while not task.done():
            if self.cancellation_provider(normalized_task_id):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise _ShutdownTaskCancelled
            await asyncio.sleep(0.1)
        return task.result()

    @staticmethod
    def _shutdown_cancelled_result() -> TaskExecutionResult:
        return TaskExecutionResult(
            False,
            "程序关闭，当前可取消任务已安全停止。",
            {"status": "cancelled", "shutdown_cancelled": True},
            cancelled=True,
        )

    def _common_browser_args(
        self,
        args: argparse.Namespace,
        settings: DesktopSettings,
        *,
        browser_endpoint: str = "",
    ) -> None:
        args.configuration_values = dict(self.configuration_provider())
        args.profile_dir = str(self._path(settings.browser_profile))
        args.log_dir = str(self._path(settings.log_dir))
        args.debug_log_dir = str(self._path(Path("debug") / "logs"))
        args.keep_browser_open = False
        args.browser_cdp_url = str(browser_endpoint or "").strip()
        args.headless = (
            os.environ.get("ERP_AUTOMATION_HEADLESS") == "1"
            and not args.browser_cdp_url
        )
        if args.headless:
            args.browser_channel = "bundled"
            args.login_timeout_sec = min(
                int(getattr(args, "login_timeout_sec", 300) or 300),
                30,
            )
        args.no_auto_login = False

    async def _process_custom_order(
        self,
        platform_order_no: str,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        confirmation: DesktopWriteConfirmation,
        *,
        task_id: str,
        browser_endpoint: str = "",
    ) -> TaskExecutionResult:
        from lingxing_automation.cli import build_parser
        from lingxing_automation.flows.contact_sync import (
            CustomOrderInteractionPolicy,
            run_retry_order,
        )
        from lingxing_automation.browser.session import (
            OrderPageAuthenticationRequired,
            OrderPageLoadFailed,
        )

        workflow_started = time.monotonic()
        self._report_progress(task_id, "正在检查订单状态与安全前置条件。", 15)
        preflight_result = await self._check_custom_order_cancellation(
            platform_order_no,
            confirmation.system_order_no,
            settings,
            configuration,
            task_id=task_id,
        )
        if preflight_result is not None:
            return preflight_result

        review_result = await self._resolve_pending_retry_review(
            settings,
            platform_order_no,
            task_id=task_id,
        )
        if review_result is not None:
            return review_result

        args = build_parser().parse_args(
            [
                "--retry-order",
                platform_order_no,
                "--apply",
                "--dedupe-path",
                str(self._path(settings.custom_state_path)),
                "--folder-root",
                settings.folder_root,
                "--batch-payment-hours",
                str(settings.payment_window_hours),
                "--allow-sku-adjustment",
                "--allow-package-split",
            ]
        )
        self._common_browser_args(
            args,
            settings,
            browser_endpoint=browser_endpoint,
        )
        args.configuration_values = dict(configuration)
        # Unlike the command-line safe-retry preset, a confirmed desktop
        # "process" action is the normal production workflow.
        args.no_dedupe_write = False
        args.no_create_folder = False
        args.resume_workflow_stages = True
        confirmed_steps: list[str] = []
        self._report_progress(task_id, "正在本机搜索并读取领星订单详情。", 25)

        async def confirm_writeback(context: dict[str, Any]) -> bool:
            self._report_progress(task_id, "联系方式已读取，等待确认差异写入。", 42)
            expected_platform = str(context.get("expected_platform_order_no") or "").strip()
            expected_system = str(context.get("expected_system_order_no") or "").strip()
            if expected_platform != confirmation.order_no:
                return False
            if confirmation.system_order_no and expected_system != confirmation.system_order_no:
                return False
            current_identity = context.get("current_identity") or {}
            before = context.get("before_values") or {}
            after = context.get("after_fill_values") or {}
            approved = await self._confirm_interaction(
                task_id=task_id,
                stage="contact_writeback",
                title="写入联系方式前确认",
                message=(
                    f"平台单号：{expected_platform}\n"
                    f"系统单号：{expected_system}\n"
                    f"当前页面系统单号：{current_identity.get('system_order_no') or '-'}\n"
                    f"电话：{before.get('phone') or '-'} → "
                    f"{after.get('phone') or context.get('phone') or '不写入'}\n"
                    f"买家邮箱：{before.get('email') or '-'} → "
                    f"{after.get('email') or context.get('email') or '不写入'}\n\n"
                    "确认后才会进入本阶段写入。"
                ),
            )
            confirmed_steps.append(
                "contact_writeback" if approved else "contact_writeback_rejected"
            )
            return approved

        async def confirm_folder(platform: str, system: str, result: Any) -> bool:
            self._report_progress(task_id, "订单资料已准备，等待确认创建文件夹。", 55)
            if platform != confirmation.order_no:
                return False
            if confirmation.system_order_no and system != confirmation.system_order_no:
                return False
            approved = await self._confirm_interaction(
                task_id=task_id,
                stage="folder_creation",
                title="创建订单文件夹前确认",
                message=self._folder_confirmation_message(platform, system, result),
            )
            confirmed_steps.append("folder_creation" if approved else "folder_creation_rejected")
            return approved

        async def confirm_plan(plan: Any) -> bool:
            if str(getattr(plan, "platform_order_no", "")) != confirmation.order_no:
                return False
            plan_name = type(plan).__name__
            is_split = "Split" in plan_name
            is_warehouse = "Warehouse" in plan_name
            stage = (
                "warehouse_logistics"
                if is_warehouse
                else ("package_split" if is_split else "sku_adjustment")
            )
            self._report_progress(
                task_id,
                (
                    "仓库物流方案已生成，等待确认。"
                    if is_warehouse
                    else "拆包方案已生成，等待确认。"
                    if is_split
                    else "SKU 调整方案已生成，等待确认。"
                ),
                82 if is_warehouse else 72 if is_split else 62,
            )
            title = (
                "设置帐篷仓库物流前确认"
                if is_warehouse
                else ("执行拆包及说明书备注前确认" if is_split else "执行 SKU 调整计划前确认")
            )
            approved = await self._confirm_interaction(
                task_id=task_id,
                stage=stage,
                title=title,
                message=(
                    self._format_warehouse_logistics_plan_for_user(plan)
                    if is_warehouse
                    else (
                        self._format_package_split_plan_for_user(plan)
                        if is_split
                        else self._format_sku_plan_for_user(plan)
                    )
                ),
            )
            confirmed_steps.append(f"{stage}_plan_approved" if approved else f"{stage}_plan_rejected")
            return approved

        async def confirm_manual_sku(platform: str, system: str, reason: str | None) -> bool:
            approved = await self._confirm_interaction(
                task_id=task_id,
                stage="manual_sku_completion",
                title="确认人工 SKU 调整已完成",
                message=(
                    f"平台单号：{platform}\n系统单号：{system}\n"
                    f"需要人工处理的原因：{reason or '-'}\n\n"
                    "仅在已经人工完成并核对无误后确认。"
                ),
                approve_label="确认已人工完成",
            )
            confirmed_steps.append(
                "manual_sku_completed" if approved else "manual_sku_requires_review"
            )
            return approved

        async def confirm_manual_split(plan: Any) -> bool:
            approved = await self._confirm_interaction(
                task_id=task_id,
                stage="manual_package_split_completion",
                title="确认人工拆包已完成",
                message=(
                    self._format_package_split_plan_for_user(plan)
                    + "\n\n仅在已经人工完成并核对无误后确认。"
                ),
                approve_label="确认已人工完成",
            )
            confirmed_steps.append(
                "manual_split_completed" if approved else "manual_split_requires_review"
            )
            return approved

        async def choose_contact(_platform: str, _system: str, contacts: list[Any]) -> Any | None:
            from lingxing_automation.parsers.contact import contact_choice_identity

            unique: list[Any] = []
            seen: set[tuple[str, str, str]] = set()
            for contact in contacts:
                identity = contact_choice_identity(contact)
                if identity is None or identity in seen:
                    continue
                seen.add(identity)
                unique.append(contact)
            if not unique:
                confirmed_steps.append("no_contact_candidate")
                return None
            # A single parsed candidate needs no separate "incomplete contact"
            # confirmation.  The real browser write still goes through the
            # detailed before/after contact-writeback review below.
            if len(unique) == 1:
                confirmed_steps.append("single_contact_selected")
                return unique[0]
            options = tuple(
                DesktopInteractionOption(
                    value=str(index),
                    label=f"候选 {index + 1}",
                    description=(
                        f"电话={getattr(contact, 'phone', None) or '-'}；"
                        f"邮箱={getattr(contact, 'email', None) or '-'}；"
                        f"来源={(getattr(contact, 'source_excerpt', None) or '-')[:160]}"
                    ),
                )
                for index, contact in enumerate(unique)
            )
            response = await self._request_interaction(
                task_id=task_id,
                stage="contact_selection",
                title="选择联系方式候选",
                message=(
                    f"平台单号：{_platform}\n系统单号：{_system}\n"
                    "请选择要写入的联系方式；信息不完整时，确认即表示只写入已有字段。"
                ),
                options=options,
                approve_label="使用所选候选",
            )
            if not response.accepted or response.selected_value is None:
                confirmed_steps.append("contact_selection_rejected")
                return None
            confirmed_steps.append("contact_candidate_selected")
            return unique[int(response.selected_value)]

        async def runtime_write_guard(stage: str, platform: str, system: str) -> bool:
            if platform != confirmation.order_no:
                confirmed_steps.append(f"write_guard_identity_rejected:{stage}")
                return False
            if confirmation.system_order_no and system != confirmation.system_order_no:
                confirmed_steps.append(f"write_guard_identity_rejected:{stage}")
                return False
            provider = self.runtime_write_guard_provider
            if provider is None:
                confirmed_steps.append(f"write_guard_missing:{stage}")
                return False
            try:
                allowed = bool(provider())
            except Exception as exc:
                confirmed_steps.append(f"write_guard_error:{stage}:{type(exc).__name__}")
                return False
            confirmed_steps.append(
                f"write_guard_allowed:{stage}" if allowed else f"write_guard_blocked:{stage}"
            )
            return allowed

        async def confirm_browser_fallback(
            operation: str,
            api_error: str,
            is_write: bool,
        ) -> bool:
            if is_write:
                safety = (
                    "API 已明确拒绝，并且返回结果能够证明本次写入尚未执行。\n"
                    "只有在此前提下才允许网页执行；结果不明确时不会出现此确认。"
                )
                title = "API 写入未执行，是否改用网页"
                approve_label = "确认改用网页写入"
            else:
                safety = "API 读取失败。确认后将使用已保留的网页流程重新读取。"
                title = "API 读取失败，是否改用网页"
                approve_label = "确认改用网页读取"
            approved = await self._confirm_interaction(
                task_id=task_id,
                stage=f"browser_fallback:{operation}",
                title=title,
                message=(
                    f"操作：{operation}\n平台单号：{confirmation.order_no}\n\n"
                    f"{safety}\n\nAPI 错误：\n{api_error}"
                ),
                approve_label=approve_label,
                reject_label="不回退，停止本阶段",
            )
            confirmed_steps.append(
                f"browser_fallback_approved:{operation}"
                if approved
                else f"browser_fallback_rejected:{operation}"
            )
            return approved

        async def confirm_instruction_remark(
            platform: str,
            system: str,
            remark: str,
        ) -> bool:
            approved = await self._confirm_interaction(
                task_id=task_id,
                stage="instruction_remark",
                title="写入说明书备注前确认",
                message=(
                    f"平台单号：{platform}\n系统单号：{system}\n"
                    f"将写入的客服备注：{remark or '-'}"
                ),
            )
            confirmed_steps.append(
                "instruction_remark_approved"
                if approved
                else "instruction_remark_rejected"
            )
            return approved

        async def capture_notification_contact(
            platform: str,
            system: str,
            recipient_name: str,
            contact: Any,
        ) -> bool:
            persisted = self._persist_customization_notification_contact(
                {
                    "platform_order_no": platform,
                    "system_order_no": system,
                    "recipient_name": recipient_name,
                    "email": str(getattr(contact, "email", None) or "").strip(),
                    "phone": str(getattr(contact, "phone", None) or "").strip(),
                    "customer_email_provided": bool(getattr(contact, "email", None)),
                    "customer_phone_provided": bool(getattr(contact, "phone", None)),
                    "contact_value_source": "customization_json",
                },
                platform_order_no=platform,
                settings=settings,
            )
            confirmed_steps.append(
                "notification_contact_captured"
                if persisted
                else "notification_contact_snapshot_unchanged"
            )
            return True

        args.custom_order_interaction_policy = CustomOrderInteractionPolicy(
            confirm_writeback=confirm_writeback,
            confirm_folder_creation=confirm_folder,
            confirm_sku_plan=confirm_plan,
            confirm_manual_sku_done=confirm_manual_sku,
            confirm_package_split_plan=confirm_plan,
            confirm_manual_package_split_done=confirm_manual_split,
            choose_contact=choose_contact,
            runtime_write_guard=runtime_write_guard,
            confirm_browser_fallback=confirm_browser_fallback,
            confirm_instruction_remark=confirm_instruction_remark,
            capture_notification_contact=capture_notification_contact,
            confirm_warehouse_logistics_plan=confirm_plan,
        )
        self._report_progress(task_id, "订单页面已就绪，正在执行本机工作流。", 32)
        try:
            if self.custom_order_api_factory is None:
                args.custom_order_api_operations = None
                payload = dict(await run_retry_order(args))
            else:
                # The API client is created inside this task's asyncio.run loop and
                # is closed before that loop ends.  Reusing an async HTTP client
                # across separate desktop task loops is not safe.
                async with self.custom_order_api_factory(settings, configuration) as operations:
                    args.custom_order_api_operations = operations
                    payload = dict(await run_retry_order(args))
        except (OrderPageAuthenticationRequired, OrderPageLoadFailed) as exc:
            message = str(exc)
            payload = {
                "status": "failed",
                "message": message,
                "shared_prerequisite_error": "lingxing_browser_session",
                "browser_session_unavailable": True,
                "updated_count": 0,
                "items": [
                    {
                        "platform_order_no": platform_order_no,
                        "status": "failed",
                        "message": message,
                    }
                ],
            }
        except Exception as exc:
            payload = {
                "status": "failed",
                "updated_count": 0,
                "items": [
                    {
                        "platform_order_no": platform_order_no,
                        "status": "failed",
                        "message": f"定制订单处理异常：{type(exc).__name__}。",
                    }
                ],
            }
        payload["desktop_confirmation_id"] = confirmation.confirmation_id
        payload["desktop_confirmed_steps"] = list(confirmed_steps)
        payload["desktop_workflow_duration_ms"] = round(
            (time.monotonic() - workflow_started) * 1000
        )
        self._report_progress(task_id, "本机流程已结束，正在保存服务器队列状态。", 95)
        return self._custom_order_result(
            payload,
            platform_order_no=platform_order_no,
            settings=settings,
        )

    async def _check_custom_order_cancellation(
        self,
        platform_order_no: str,
        system_order_no: str,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        *,
        task_id: str,
    ) -> TaskExecutionResult | None:
        """Re-read the exact order before any stage can perform a write."""

        if self.custom_order_status_check is None:
            return None
        try:
            status = await self.custom_order_status_check(
                settings,
                configuration,
                platform_order_no,
                system_order_no,
            )
        except Exception as exc:
            message = (
                "处理前无法读取领星订单最新状态，已停止本次处理以避免误写："
                f"{type(exc).__name__}。"
            )
            return self._custom_order_result(
                {
                    "status": "failed",
                    "updated_count": 0,
                    "items": [
                        {
                            "platform_order_no": platform_order_no,
                            "status": "order_status_check_failed",
                            "message": message,
                        }
                    ],
                },
                platform_order_no=platform_order_no,
                settings=settings,
            )

        if not bool(getattr(status, "buyer_cancel_requested", False)):
            return None

        message = (
            f"平台单号 {platform_order_no} 的领星状态已变为“买家申请取消”。"
            "本地定制工作流已改为“不需要”，本次及后续阶段均不再处理。"
        )
        try:
            from erp_automation.persistence import CustomWorkflowStore

            store = CustomWorkflowStore(self._path(settings.custom_state_path))
            summary = store.mark_workflows_not_required(
                [platform_order_no],
                reason="处理订单前实时复查发现买家申请取消。",
                actor="desktop_worker",
            )
        except Exception as exc:
            failure_message = (
                "已确认订单为买家申请取消，但本地工作流未能安全改为不需要："
                f"{type(exc).__name__}。本次处理已停止。"
            )
            return TaskExecutionResult(
                False,
                failure_message,
                {
                    "status": "failed",
                    "platform_order_no": platform_order_no,
                    "message": failure_message,
                },
            )

        await self._request_interaction(
            task_id=task_id,
            stage="buyer_cancelled",
            title="订单已申请取消",
            message=message,
            approve_label="知道了",
            reject_label="关闭提示",
        )
        return TaskExecutionResult(
            True,
            message,
            {
                "status": "not_required",
                "platform_order_no": platform_order_no,
                "system_order_no": system_order_no,
                "buyer_cancel_requested": True,
                "workflow_status": "not_required",
                "changed_order_count": summary.changed_order_count,
            },
        )

    async def _query_logistics(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        *,
        task_id: str = "",
        browser_endpoint: str = "",
    ) -> TaskExecutionResult:
        normalized_endpoint = str(browser_endpoint or "").strip()
        if (
            os.environ.get("ERP_AUTOMATION_HEADLESS") == "1"
            and not normalized_endpoint
        ):
            return TaskExecutionResult(
                False,
                (
                    "阿里物流网页只能由在线客户端的本机可见 Chrome 查询。"
                    "当前服务器没有客户端浏览器通道，物流记录保持待查询。"
                ),
                {
                    "status": "waiting_for_local_browser",
                    "local_visible_browser_required": True,
                    "alibaba_logistics_query_count": 0,
                },
                blocked=True,
            )
        from shipment_automation.cli import build_parser
        from shipment_automation.logistics_worker import run_logistics_worker

        args = build_parser().parse_args(
            [
                "logistics",
                "--from-queue",
                "--update-queue",
                "--queue-path",
                str(self._path(settings.queue_path)),
                "--profile-dir",
                str(self._path(settings.browser_profile)),
            ]
        )
        self._common_browser_args(
            args,
            settings,
            browser_endpoint=normalized_endpoint,
        )
        args.configuration_values = dict(configuration)
        args.progress_callback = lambda message, percent: self._report_progress(
            task_id,
            message,
            percent,
        )
        self._report_progress(
            task_id,
            "正在读取到期物流队列并准备本机可见 Chrome。",
            12,
        )
        payload = dict(await run_logistics_worker(args))
        return self._result(payload, success_statuses={"completed", "completed_with_skips"})

    async def _mark_shipment(
        self,
        logistics_no: str,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        confirmation: DesktopWriteConfirmation,
        *,
        task_id: str,
        auto_send_customer_notification: bool = False,
        browser_endpoint: str = "",
    ) -> TaskExecutionResult:
        from shipment_automation.cli import build_parser
        from shipment_automation.erp_mark_ship import (
            ErpMarkEmergencyStopped,
            ErpMarkUserAbort,
            run_erp_mark_worker,
        )

        workflow_started = time.monotonic()
        self._report_progress(task_id, "正在读取自动标发队列并准备领星 API。", 20)
        args = build_parser().parse_args(
            [
                "erp-mark",
                "--execute",
                "--queue-path",
                str(self._path(settings.queue_path)),
                "--profile-dir",
                str(self._path(settings.browser_profile)),
            ]
        )
        self._common_browser_args(
            args,
            settings,
            browser_endpoint=browser_endpoint,
        )
        args.configuration_values = dict(configuration)
        args.logistics_no = logistics_no
        args.email_preview_enabled = email_preview_enabled(configuration)
        confirmation_hashes: list[str] = []
        auto_approved_hashes: list[str] = []
        user_confirmed_hashes: list[str] = []

        def task_cancellation_requested() -> bool:
            if self.cancellation_provider is None or not task_id:
                return False
            try:
                return bool(self.cancellation_provider(task_id))
            except Exception:
                return True

        async def runtime_guard() -> bool:
            if task_cancellation_requested():
                return False
            provider = self.runtime_write_guard_provider
            if provider is None:
                return True
            try:
                return bool(provider())
            except Exception:
                return False

        async def desktop_confirm(prompt: str) -> bool:
            if not await runtime_guard():
                raise ErpMarkEmergencyStopped(
                    (
                        "本轮自动标发处理已取消；当前原子步骤结束后停止，后续阶段保持待处理。"
                        if task_cancellation_requested()
                        else "已触发紧急停止；当前标发阶段保持待处理。"
                    )
                )
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            confirmation_hashes.append(prompt_hash)
            operation_match = re.search(r"【([^】]+)】", prompt)
            operation = operation_match.group(1) if operation_match else "写入检查点"
            is_fallback = "改用原网页流程" in prompt
            self._report_progress(
                task_id,
                (
                    "领星 API 明确拒绝，等待确认是否启动本机 Chrome 回退。"
                    if is_fallback
                    else f"自动标发正在执行：{operation}。"
                ),
                70 if is_fallback else 45,
            )
            # Treat the waybill review as a dedicated write boundary.  The
            # field check keeps the desktop guard effective even if the worker
            # wording changes while the actual request schema stays the same.
            is_waybill_review = (
                operation in {
                    "审核运单填写信息",
                    "审核快速出库运单信息",
                }
                or (
                    "waybill_no" in prompt
                    and "tracking_no" in prompt
                    and "pkg_fee_weight" in prompt
                )
            )
            if not is_fallback and not is_waybill_review:
                # Clicking "执行勾选标发" authorizes the routine ERP stages.
                # They still pass the runtime guard and are audited, but no
                # longer create repetitive modal confirmation requests.
                auto_approved_hashes.append(prompt_hash)
                desktop_confirm.confirmation_id = confirmation.confirmation_id  # type: ignore[attr-defined]
                desktop_confirm.confirmation_source = confirmation.source  # type: ignore[attr-defined]
                return True
            interaction_stage = (
                "erp_mark:browser_fallback"
                if is_fallback
                else (
                    "erp_mark:waybill_review"
                    if is_waybill_review
                    else f"erp_mark:{operation}"
                )
            )
            response = await self._request_interaction(
                task_id=task_id,
                stage=interaction_stage,
                title=(
                    "API 明确未执行，是否改用网页标发"
                    if is_fallback
                    else (
                        (
                            "审核快速出库运单信息"
                            if operation == "审核快速出库运单信息"
                            else "审核运单填写信息"
                        )
                        if is_waybill_review
                        else f"ERP 标发阶段确认：{operation}"
                    )
                ),
                message=prompt,
                approve_label=(
                    "确认改用网页"
                    if is_fallback
                    else (
                        "确认写入运单"
                        if is_waybill_review
                        else "确认执行本阶段"
                    )
                ),
            )
            if not response.accepted and not await runtime_guard():
                raise ErpMarkEmergencyStopped(
                    (
                        "本轮自动标发处理已取消；当前原子步骤结束后停止，后续阶段保持待处理。"
                        if task_cancellation_requested()
                        else "已触发紧急停止；当前标发阶段保持待处理。"
                    )
                )
            if response.accepted:
                user_confirmed_hashes.append(prompt_hash)
                desktop_confirm.confirmation_id = response.request_id  # type: ignore[attr-defined]
                desktop_confirm.confirmation_source = "qt_message_box"  # type: ignore[attr-defined]
            return bool(response.accepted)

        async def select_wms_row(item: Any, candidates: list[dict[str, Any]]) -> str:
            options: list[DesktopInteractionOption] = []
            for candidate in candidates:
                wo_number = str(candidate.get("wo_number") or "").strip()
                if not wo_number:
                    continue
                status = str(
                    candidate.get("status_name") or candidate.get("status") or "-"
                ).strip()
                warehouse = str(
                    candidate.get("warehouse_name")
                    or candidate.get("warehouse")
                    or candidate.get("wid")
                    or "-"
                ).strip()
                logistics = str(
                    candidate.get("logistics_type_name")
                    or candidate.get("logistics_name")
                    or "-"
                ).strip()
                waybill = str(candidate.get("waybill_no") or "-").strip()
                tracking = str(candidate.get("tracking_no") or "-").strip()
                updated = str(
                    candidate.get("updated_at")
                    or candidate.get("update_time")
                    or candidate.get("gmt_modified")
                    or "-"
                ).strip()
                options.append(
                    DesktopInteractionOption(
                        value=wo_number,
                        label=f"销售出库单 {wo_number}",
                        description=(
                            f"状态 {status}；仓库 {warehouse}；物流 {logistics}；"
                            f"运单 {waybill}；跟踪 {tracking}；更新 {updated}"
                        ),
                    )
                )
            if len(options) != len(candidates) or len({item.value for item in options}) != len(options):
                raise ErpMarkUserAbort(
                    "销售出库单候选缺少唯一 wo_number，无法安全选择。"
                )
            response = await self._request_interaction(
                task_id=task_id,
                stage="erp_mark:wms_outbound_select",
                title="选择要继续标发的销售出库单",
                message=(
                    f"系统单号：{getattr(item, 'system_order_no', '-') or '-'}\n"
                    f"平台单号：{getattr(item, 'platform_order_no', '-') or '-'}\n\n"
                    "领星返回多条销售出库单。请根据仓库、物流方式和现有单号"
                    "明确选择一条；选择前不会执行 ERP 写入。"
                ),
                options=tuple(options),
                approve_label="选定并继续标发",
                reject_label="取消本次标发",
            )
            selected = str(response.selected_value or "").strip()
            if not response.accepted or selected not in {option.value for option in options}:
                raise ErpMarkUserAbort("用户未选择销售出库单。")
            return selected

        # The queue worker reads these attributes and records one audit event
        # for every approved dangerous-write boundary without storing the
        # potentially sensitive prompt text itself.
        desktop_confirm.confirmation_id = confirmation.confirmation_id  # type: ignore[attr-defined]
        desktop_confirm.confirmation_source = confirmation.source  # type: ignore[attr-defined]
        desktop_confirm.select_wms_row = select_wms_row  # type: ignore[attr-defined]
        args.confirm_func = desktop_confirm
        args.runtime_guard_func = runtime_guard
        lazy_browser_state: dict[str, Any] = {}
        if self.erp_mark_func is not None:
            if bool(
                getattr(
                    self.erp_mark_func,
                    "supports_lazy_browser_fallback",
                    False,
                )
            ):
                from lingxing_automation.browser.session import (
                    get_first_page,
                    launch_context,
                    wait_for_order_page,
                )
                from lingxing_automation.config import (
                    configuration_source_from_args,
                    load_login_config,
                )
                from lingxing_automation.constants import ORDER_MANAGEMENT_URL
                from lingxing_automation.pages.order_detail import (
                    close_order_detail_dialog,
                )
                from lingxing_automation.pages.order_management import (
                    ensure_order_view_mode,
                )

                async def fallback_page_provider():
                    current = lazy_browser_state.get("page")
                    if current is not None:
                        try:
                            if not current.is_closed():
                                return current
                        except Exception:
                            pass
                    playwright, context = await launch_context(args)
                    page = await get_first_page(context)
                    lazy_browser_state.update(
                        playwright=playwright,
                        context=context,
                        page=page,
                    )
                    login_config = load_login_config(
                        configuration_source_from_args(args)
                    )
                    if "mpOrderManagement" not in page.url:
                        await page.goto(
                            ORDER_MANAGEMENT_URL,
                            wait_until="domcontentloaded",
                        )
                    await wait_for_order_page(
                        page,
                        int(getattr(args, "login_timeout_sec", 300)),
                        login_config,
                        auto_login=True,
                        debug_dir=getattr(args, "debug_log_dir", "debug/logs"),
                    )
                    if "mpOrderManagement" not in page.url:
                        await page.goto(
                            ORDER_MANAGEMENT_URL,
                            wait_until="domcontentloaded",
                        )
                        await wait_for_order_page(
                            page,
                            int(getattr(args, "login_timeout_sec", 300)),
                            login_config,
                            auto_login=True,
                            debug_dir=getattr(
                                args,
                                "debug_log_dir",
                                "debug/logs",
                            ),
                        )
                    await close_order_detail_dialog(page)
                    await ensure_order_view_mode(
                        page,
                        debug_dir=getattr(args, "debug_log_dir", "debug/logs"),
                    )
                    return page

                base_mark_func = self.erp_mark_func

                async def lazy_mark_item(
                    page,
                    item,
                    confirm_func,
                    checkpoint_func=None,
                    approval_func=None,
                    runtime_guard_func=None,
                ):
                    return await base_mark_func(
                        page,
                        item,
                        confirm_func,
                        checkpoint_func,
                        approval_func,
                        runtime_guard_func,
                        browser_page_provider=fallback_page_provider,
                    )

                lazy_mark_item.requires_browser_fallback = False  # type: ignore[attr-defined]
                lazy_mark_item.manages_checkpoints = bool(  # type: ignore[attr-defined]
                    getattr(base_mark_func, "manages_checkpoints", False)
                )
                lazy_mark_item.supports_runtime_guard = bool(  # type: ignore[attr-defined]
                    getattr(base_mark_func, "supports_runtime_guard", False)
                )
                args.mark_item_func = lazy_mark_item
            else:
                args.mark_item_func = self.erp_mark_func
        try:
            payload = dict(await run_erp_mark_worker(args))
        finally:
            context = lazy_browser_state.get("context")
            playwright = lazy_browser_state.get("playwright")
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    pass
        payload["erp_mark_duration_ms"] = round(
            (time.monotonic() - workflow_started) * 1000
        )
        payload["erp_mark_browser_fallback_used"] = bool(lazy_browser_state)
        payload["desktop_confirmation_id"] = confirmation.confirmation_id
        payload["desktop_confirmed_prompt_hashes"] = confirmation_hashes
        payload["desktop_auto_approved_prompt_hashes"] = auto_approved_hashes
        payload["desktop_user_confirmed_prompt_hashes"] = user_confirmed_hashes
        if payload.get("paused_count") or str(payload.get("status") or "") == "cancelled":
            return TaskExecutionResult(
                False,
                str(payload.get("message") or "ERP 标发已按紧急停止安全暂停。"),
                payload,
                cancelled=True,
            )
        result = self._result(payload, success_statuses={"completed", "completed_with_skips"})
        if payload.get("blocked_count") or payload.get("manual_review_count"):
            return TaskExecutionResult(False, result.message, payload, blocked=True)
        if (
            not result.succeeded
            or int(payload.get("done_count") or 0) <= 0
            or self.shipment_notification_sync is None
        ):
            self._report_progress(task_id, "自动标发阶段已结束，正在保存队列结果。", 95)
            return result

        notification_sync_started = time.monotonic()
        self._report_progress(task_id, "标发已完成，正在通过 API 同步客户通知物流。", 88)
        try:
            sync_value = await self.shipment_notification_sync(
                settings,
                configuration,
                task_id,
                (confirmation.order_no,),
            )
            sync_report = dict(sync_value)
            payload["notification_sync"] = dict(
                sync_report.get("notification_sync") or {}
            )
            payload["notification_sync_external_provider_calls"] = int(
                sync_report.get("external_provider_calls") or 0
            )
            payload["notification_sync_duration_ms"] = round(
                (time.monotonic() - notification_sync_started) * 1000
            )
            failed_notification_count = int(
                payload["notification_sync"].get("failed_order_count") or 0
            )
            if failed_notification_count:
                payload["notification_sync_warning"] = (
                    f"客户通知物流同步失败订单 {failed_notification_count}，"
                    "后续扫描将自动补偿。"
                )
                return TaskExecutionResult(
                    True,
                    f"{result.message}；{payload['notification_sync_warning']}",
                    payload,
                )
        except Exception as exc:  # ERP completion must remain committed.
            payload["notification_sync_duration_ms"] = round(
                (time.monotonic() - notification_sync_started) * 1000
            )
            payload["notification_sync_warning"] = str(exc)
            return TaskExecutionResult(
                True,
                f"{result.message}；客户通知物流自动同步失败，将由定时扫描补偿。",
                payload,
            )
        if auto_send_customer_notification:
            if self.shipment_notification_send is None:
                payload["customer_notification_send_warning"] = (
                    "客户通知发送器尚未连接，请到“客户通知审核”页面发送。"
                )
                return TaskExecutionResult(
                    True,
                    f"{result.message}；{payload['customer_notification_send_warning']}",
                    payload,
                )
            self._report_progress(task_id, "物流已同步，正在发送客户通知。", 93)
            try:
                send_value = await self.shipment_notification_send(
                    settings,
                    configuration,
                    task_id,
                    (confirmation.order_no,),
                )
                send_report = dict(send_value)
                payload["customer_notification_send"] = send_report
                failed_send_count = int(send_report.get("failed_count") or 0)
                if failed_send_count:
                    payload["customer_notification_send_warning"] = str(
                        send_report.get("message")
                        or f"客户通知有 {failed_send_count} 条未发送。"
                    )
                    return TaskExecutionResult(
                        True,
                        f"{result.message}；{payload['customer_notification_send_warning']}",
                        payload,
                    )
            except Exception as exc:
                payload["customer_notification_send_warning"] = (
                    f"客户通知发送未完成（{type(exc).__name__}），"
                    "请到“客户通知审核”页面处理。"
                )
                return TaskExecutionResult(
                    True,
                    f"{result.message}；{payload['customer_notification_send_warning']}",
                    payload,
                )
        self._report_progress(task_id, "自动标发及客户通知同步已完成。", 95)
        return TaskExecutionResult(True, result.message, payload)

    async def _request_interaction(
        self,
        *,
        task_id: str,
        stage: str,
        title: str,
        message: str,
        options: tuple[DesktopInteractionOption, ...] = (),
        approve_label: str = "确认执行",
        reject_label: str = "拒绝 / 停止",
    ) -> DesktopInteractionResponse:
        if self.interaction_handler is None:
            return DesktopInteractionResponse("unavailable", False)
        return await self.interaction_handler(
            task_id=task_id,
            stage=stage,
            title=title,
            message=message,
            options=options,
            approve_label=approve_label,
            reject_label=reject_label,
        )

    async def _confirm_interaction(self, **kwargs: Any) -> bool:
        return bool((await self._request_interaction(**kwargs)).accepted)

    @staticmethod
    def _display_payload(value: Any) -> str:
        if hasattr(value, "to_log_dict"):
            payload = value.to_log_dict()
        elif is_dataclass(value):
            payload = asdict(value)
        elif isinstance(value, Mapping):
            payload = dict(value)
        else:
            payload = {"value": str(value)}
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)[:12000]

    @staticmethod
    def _format_sku_plan_for_user(plan: Any) -> str:
        lines = [
            f"平台单号：{getattr(plan, 'platform_order_no', '-') or '-'}",
            f"系统单号：{getattr(plan, 'system_order_no', '-') or '-'}",
        ]
        destination = getattr(plan, "destination", None)
        if destination is not None:
            category = str(getattr(destination, "category", "") or "unknown")
            category_label = {
                "us_mainland": "美国本土",
                "us_non_mainland": "美国非本土地区",
                "canada": "加拿大",
                "unknown": "未识别",
            }.get(category, category)
            location = " / ".join(
                value
                for value in (
                    str(getattr(destination, "country", "") or "").strip(),
                    str(getattr(destination, "state", "") or "").strip(),
                    str(getattr(destination, "city", "") or "").strip(),
                )
                if value
            )
            lines.append(
                f"收货地区：{category_label}{f'（{location}）' if location else ''}"
            )

        replacements = list(getattr(plan, "replace_main_items", None) or [])
        if not replacements and getattr(plan, "replace_main_sku", None):
            replacements = [
                SimpleNamespace(
                    source_sku=None,
                    sku=getattr(plan, "replace_main_sku", None),
                    quantity=getattr(plan, "replace_main_quantity", 1),
                    reason="",
                )
            ]
        additions = list(getattr(plan, "add_items", None) or [])
        if replacements:
            lines.extend(["", "替换商品："])
            for index, item in enumerate(replacements, start=1):
                source_sku = str(getattr(item, "source_sku", "") or "当前主商品")
                target_sku = str(getattr(item, "sku", "") or "-")
                quantity = int(getattr(item, "quantity", 1) or 1)
                reason = str(getattr(item, "reason", "") or "").strip()
                suffix = f"（{reason}）" if reason else ""
                lines.append(
                    f"  {index}. {source_sku} → {target_sku} × {quantity}{suffix}"
                )
        if additions:
            lines.extend(["", "新增商品："])
            for index, item in enumerate(additions, start=1):
                sku = str(getattr(item, "sku", "") or "-")
                quantity = int(getattr(item, "quantity", 1) or 1)
                reason = str(getattr(item, "reason", "") or "").strip()
                suffix = f"（{reason}）" if reason else ""
                lines.append(f"  {index}. {sku} × {quantity}{suffix}")
        if not replacements and not additions:
            lines.extend(["", "本阶段没有需要修改的 SKU。"])

        manual_reason = str(getattr(plan, "manual_reason", "") or "").strip()
        if bool(getattr(plan, "manual_required", False)):
            lines.extend(["", f"需要人工处理：{manual_reason or '-'}"])
        warnings = [str(value) for value in (getattr(plan, "warnings", None) or []) if value]
        if warnings:
            lines.extend(["", f"警告：{'；'.join(warnings)}"])
        return "\n".join(lines)

    @staticmethod
    def _format_package_split_plan_for_user(plan: Any) -> str:
        lines = [
            f"平台单号：{getattr(plan, 'platform_order_no', '-') or '-'}",
            f"系统单号：{getattr(plan, 'system_order_no', '-') or '-'}",
        ]
        reason = str(getattr(plan, "reason", "") or "").strip()
        if reason:
            lines.append(f"拆包原因：{reason}")
        packages = list(getattr(plan, "packages_to_split", None) or [])
        if packages:
            lines.extend(["", f"将拆出 {len(packages)} 个新包裹："])
            for package_index, package in enumerate(packages, start=1):
                title = str(getattr(package, "title", "") or f"包裹 {package_index}")
                lines.append(f"  {package_index}. {title}")
                for item in list(getattr(package, "items", None) or []):
                    sku = str(getattr(item, "sku", "") or "-")
                    quantity = int(getattr(item, "quantity", 1) or 1)
                    item_reason = str(getattr(item, "reason", "") or "").strip()
                    suffix = f"（{item_reason}）" if item_reason else ""
                    lines.append(f"     • {sku} × {quantity}{suffix}")
            lines.extend(["", "其余商品保留在原包裹中。"])
        else:
            lines.extend(["", "无需拆出新包裹。"])
        remark = str(getattr(plan, "customer_remark", "") or "").strip()
        if remark:
            lines.extend(["", f"说明书客服备注：{remark}"])
        manual_reason = str(getattr(plan, "manual_reason", "") or "").strip()
        if bool(getattr(plan, "manual_required", False)):
            lines.extend(["", f"需要人工处理：{manual_reason or '-'}"])
        warnings = [str(value) for value in (getattr(plan, "warnings", None) or []) if value]
        if warnings:
            lines.extend(["", f"警告：{'；'.join(warnings)}"])
        return "\n".join(lines)

    @staticmethod
    def _format_warehouse_logistics_plan_for_user(plan: Any) -> str:
        lines = [
            f"平台单号：{getattr(plan, 'platform_order_no', '-') or '-'}",
            f"目的邮编：{getattr(plan, 'postal_code', '-') or '-'}",
            "本阶段只设置领星仓库物流，不购买亚马逊面单。",
            "",
        ]
        for index, decision in enumerate(
            list(getattr(plan, "decisions", None) or []), start=1
        ):
            system_order_no = str(
                getattr(decision, "system_order_no", "") or "-"
            )
            skus = ", ".join(getattr(decision, "skus", None) or []) or "-"
            warehouse = str(
                getattr(decision, "target_warehouse_name", "") or "保持不变"
            )
            channel = str(
                getattr(decision, "target_channel_name", "") or "保持不变"
            )
            reason = str(getattr(decision, "reason", "") or "-")
            lines.extend(
                [
                    f"{index}. 系统单号 {system_order_no}",
                    f"   SKU：{skus}",
                    f"   目标：{warehouse} / {channel}",
                    f"   原因：{reason}",
                ]
            )
        return "\n".join(lines)

    @staticmethod
    def _folder_confirmation_message(platform: str, system: str, result: Any) -> str:
        components = list(getattr(result, "folder_components", None) or [])
        warnings = list(getattr(result, "folder_warnings", None) or [])
        component_lines = "\n".join(
            f"  {index}. {value}"
            for index, value in enumerate(components, start=1)
        ) or "  -"
        actual_name = str(getattr(result, "folder_name", "") or "-")
        full_name = str(getattr(result, "folder_name_full", "") or actual_name)
        name_lines = (
            f"完整文件夹名：{full_name}\n\n实际文件夹名：{actual_name}"
            if bool(getattr(result, "folder_name_was_shortened", False))
            else f"文件夹名：{actual_name}"
        )
        return (
            f"平台单号：{platform}\n"
            f"系统单号：{system}\n"
            f"文件夹状态：{getattr(result, 'status', '-') or '-'}\n"
            f"付款时间：{getattr(result, 'payment_time', '-') or '-'}\n"
            f"文件夹日期：{getattr(result, 'folder_date', '-') or '-'}"
            f"（来源：{getattr(result, 'folder_date_source', '-') or '-'}）\n"
            f"{name_lines}\n"
            f"完整路径：{getattr(result, 'folder_path', '-') or '-'}\n"
            f"组件：\n{component_lines}\n"
            f"警告：{'；'.join(str(value) for value in warnings) or '-'}"
        )

    def _consume_confirmation(
        self,
        command: TaskCommand,
        action: DesktopWriteAction,
        *,
        system_order_no: str = "",
        logistics_no: str = "",
    ) -> DesktopWriteConfirmation:
        confirmation = DesktopWriteConfirmation.from_payload(command.payload)
        confirmation.require_matches(
            action,
            command.order_no or "",
            system_order_no=system_order_no,
            logistics_no=logistics_no,
        )
        if confirmation.confirmation_id in self._consumed_confirmation_ids:
            raise ValueError("该桌面写入确认已经使用；请返回订单页面重新确认。")
        self._consumed_confirmation_ids.add(confirmation.confirmation_id)
        return confirmation

    async def _resolve_pending_retry_review(
        self,
        settings: DesktopSettings,
        platform_order_no: str,
        *,
        task_id: str,
    ) -> TaskExecutionResult | None:
        state_path = self._path(settings.custom_state_path)
        if state_path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
            return None
        from erp_automation.persistence import (
            CustomWorkflowStore,
            StageRetryReviewResolution,
        )

        store = CustomWorkflowStore(state_path)
        review = store.get_pending_retry_review(platform_order_no)
        if review is None:
            return None
        stage = str(review.get("stage") or "")
        stage_labels = {
            "contact": "联系方式",
            "folder": "订单文件夹",
            "sku": "SKU 调整",
            "package_split": "拆包",
            "instruction_remark": "说明书备注",
        }
        response = await self._request_interaction(
            task_id=task_id,
            stage=f"retry_review:{stage}",
            title="写入结果待人工复核",
            message=(
                f"平台单号：{platform_order_no}\n"
                f"待复核阶段：{stage_labels.get(stage, stage)}\n"
                f"上次错误：{review.get('last_error') or '-'}\n\n"
                "上次写入结果无法判断。请先在领星 ERP 中核对，未经确认不会重复执行。"
            ),
            options=(
                DesktopInteractionOption(
                    value=str(StageRetryReviewResolution.RETRY),
                    label="确认未执行，重新处理",
                    description="已在 ERP 核实上次写入未生效，本次可以安全重试该阶段。",
                ),
                DesktopInteractionOption(
                    value=str(StageRetryReviewResolution.COMPLETED),
                    label="确认已执行，标记本阶段完成",
                    description="已在 ERP 核实写入成功，本次跳过该阶段并继续后续阶段。",
                ),
            ),
            approve_label="确认复核结果",
            reject_label="暂不确认，保持待处理",
        )
        selected = str(response.selected_value or "").strip()
        allowed = {
            str(StageRetryReviewResolution.RETRY),
            str(StageRetryReviewResolution.COMPLETED),
        }
        if not response.accepted or selected not in allowed:
            payload = {
                "status": "cancelled",
                "platform_order_no": platform_order_no,
                "workflow_paused_stage": stage,
                "retry_review_required": True,
            }
            return TaskExecutionResult(
                False,
                "人工复核尚未确认，阶段保持待处理且不会重复写入。",
                payload,
                cancelled=True,
            )
        workflow_status = store.resolve_stage_retry_review(
            platform_order_no,
            stage,
            selected,
            reason=(
                "用户确认上次写入未执行，允许重新处理。"
                if selected == str(StageRetryReviewResolution.RETRY)
                else "用户已在 ERP 核实上次写入成功。"
            ),
            actor="desktop_user",
        )
        if workflow_status == "completed":
            return TaskExecutionResult(
                True,
                "人工复核确认本阶段已执行，订单工作流现已完成。",
                {
                    "status": "completed",
                    "platform_order_no": platform_order_no,
                    "workflow_status": workflow_status,
                    "retry_review_resolution": selected,
                },
            )
        return None

    def _persist_customization_notification_contact(
        self,
        item: Mapping[str, Any],
        *,
        platform_order_no: str,
        settings: DesktopSettings,
    ) -> bool:
        """Persist the authoritative customization-JSON contact snapshot.

        This deliberately does not depend on ERP writeback or later folder/SKU
        stages.  Empty JSON fields are persisted as empty so no downstream
        Lingxing/WMS value can silently become a notification destination.
        """

        if str(item.get("contact_value_source") or "").strip() != "customization_json":
            return False

        from shipment_automation.notification_store import ShipmentNotificationStore

        store = ShipmentNotificationStore(self._path(settings.queue_path))
        email = str(item.get("email") or "").strip()
        phone = str(item.get("phone") or "").strip()
        system_order_no = str(item.get("system_order_no") or "").strip()
        return store.upsert_customization_contact(
            platform_order_no,
            email=email,
            phone=phone,
            system_order_nos=([system_order_no] if system_order_no else ()),
        )

    def _custom_order_result(
        self,
        payload: dict[str, Any],
        *,
        platform_order_no: str,
        settings: DesktopSettings,
    ) -> TaskExecutionResult:
        items = [item for item in payload.get("items") or [] if isinstance(item, Mapping)]
        item = dict(items[0]) if len(items) == 1 else {}
        try:
            payload["shipment_notification_contact_persisted"] = (
                self._persist_customization_notification_contact(
                    item,
                    platform_order_no=platform_order_no,
                    settings=settings,
                )
            )
        except Exception as exc:
            payload["shipment_notification_contact_persisted"] = False
            payload["shipment_notification_contact_persist_error"] = type(exc).__name__
        item_status = str(item.get("status") or "").strip().lower()
        strictly_completed = (
            str(payload.get("status") or "").strip().lower() == "completed"
            and int(payload.get("updated_count") or 0) == 1
            and len(items) == 1
            and item_status == "updated"
            and not self._contains_unresolved_write(item)
        )
        if strictly_completed:
            return TaskExecutionResult(True, "定制订单处理完成。", payload)

        original_status = str(payload.get("status") or "unknown")
        message = str(item.get("message") or payload.get("message") or "").strip()
        if not message:
            message = (
                f"定制订单未得到可证明的完整成功结果（顶层={original_status}，"
                f"订单={item_status or 'missing'}），当前阶段已暂停。"
            )
        pause_kind = self._custom_pause_kind(payload, item)
        payload["original_status"] = original_status
        payload["platform_order_no"] = platform_order_no
        if pause_kind == "ambiguous_write":
            payload["status"] = "manual_review"
            payload["manual_review_required"] = True
        elif pause_kind in {"user_cancelled", "emergency_stop"}:
            payload["status"] = "cancelled"
            payload["manual_review_required"] = False
        else:
            payload["status"] = "failed"
            payload["manual_review_required"] = False
        payload["message"] = message
        stage = self._paused_custom_stage(item)
        payload["workflow_paused_stage"] = stage
        self._record_custom_workflow_paused(
            settings,
            platform_order_no,
            stage=stage,
            reason=message,
            result_status=item_status or original_status,
            pause_kind=pause_kind,
            payload=payload,
        )
        return TaskExecutionResult(
            False,
            message,
            payload,
            blocked=pause_kind == "ambiguous_write",
            cancelled=pause_kind in {"user_cancelled", "emergency_stop"},
        )

    @staticmethod
    def _contains_unresolved_write(item: Mapping[str, Any]) -> bool:
        for key, value in item.items():
            normalized_key = str(key).strip().lower()
            if "manual_review" in normalized_key and bool(value):
                return True
            if (
                normalized_key.endswith("_error")
                and value is not None
                and value != ""
                and value != []
                and value != {}
            ):
                return True
            if normalized_key.endswith("_status") or normalized_key == "status":
                status = str(value or "").strip().lower()
                if any(
                    token in status
                    for token in (
                        "unknown",
                        "manual_review",
                        "manual_pending",
                        "failed",
                        "error",
                        "cancelled",
                        "needs_manual",
                        "write_disabled",
                        "blocked",
                    )
                ):
                    return True
        return False

    @staticmethod
    def _paused_custom_stage(item: Mapping[str, Any]) -> str:
        if item.get("warehouse_logistics_status") or item.get("warehouse_logistics_error"):
            return "warehouse_logistics"
        if item.get("instruction_remark_status") or item.get("instruction_remark_error"):
            return "instruction_remark"
        if item.get("package_split_status") or item.get("package_split_error"):
            return "package_split"
        if item.get("sku_adjustment_status") or item.get("sku_adjustment_error"):
            return "sku"
        if item.get("folder_status") or item.get("folder_error"):
            return "folder"
        return "contact"

    @classmethod
    def _custom_pause_kind(cls, payload: Mapping[str, Any], item: Mapping[str, Any]) -> str:
        steps = [str(value).lower() for value in payload.get("desktop_confirmed_steps") or []]
        if any(
            value.startswith("write_guard_") and "write_guard_allowed:" not in value
            for value in steps
        ):
            return "emergency_stop"
        if any(
            value.endswith("_rejected")
            or "_rejected:" in value
            or value.endswith("_requires_review")
            for value in steps
        ):
            return "user_cancelled"
        item_status = str(item.get("status") or "").strip().lower()
        if any(
            token in item_status
            for token in (
                "user_cancelled",
                "contact_choice_skipped",
                "folder_creation_cancelled",
            )
        ):
            return "user_cancelled"
        if any(token in item_status for token in ("write_blocked", "emergency_stop")):
            return "emergency_stop"
        if cls._contains_ambiguous_write(item) or cls._contains_ambiguous_write(payload):
            return "ambiguous_write"
        return "retryable_failure"

    @classmethod
    def _contains_ambiguous_write(cls, value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized_key = str(key).strip().lower()
                if "manual_review" in normalized_key and bool(nested):
                    return True
                if normalized_key.endswith("_status") or normalized_key == "status":
                    status = str(nested or "").strip().lower()
                    if any(
                        token in status
                        for token in ("unknown", "manual_review", "manual_pending", "needs_manual")
                    ):
                        return True
                if cls._contains_ambiguous_write(nested):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(cls._contains_ambiguous_write(item) for item in value)
        return False

    def _record_custom_workflow_paused(
        self,
        settings: DesktopSettings,
        platform_order_no: str,
        *,
        stage: str,
        reason: str,
        result_status: str,
        pause_kind: str,
        payload: dict[str, Any],
    ) -> None:
        state_path = self._path(settings.custom_state_path)
        if state_path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
            payload["workflow_pause_recorded"] = False
            return
        try:
            from erp_automation.persistence import CustomWorkflowStore

            store = CustomWorkflowStore(state_path)
            if store.get_workflow(platform_order_no) is None:
                store.mutate_legacy_record(
                    platform_order_no,
                    lambda current: {**current, "workflow_status": "pending"},
                    event_type="desktop_processing_paused_initialized",
                    actor="desktop_worker",
                    reason=reason,
                )
            record = store.record_workflow_paused(
                platform_order_no,
                stage,
                reason=reason,
                result_status=result_status,
                pause_kind=pause_kind,
                actor="desktop_worker",
            )
            payload["workflow_paused_stage"] = record.stage
            payload["workflow_pause_recorded"] = True
            payload["workflow_status"] = record.workflow_status
        except Exception as exc:
            payload["workflow_pause_recorded"] = False
            payload["workflow_pause_error_type"] = type(exc).__name__

    def _path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.workspace / path

    @staticmethod
    def _result(
        payload: Mapping[str, Any],
        *,
        success_statuses: set[str],
    ) -> TaskExecutionResult:
        status = str(payload.get("status") or "")
        succeeded = status in success_statuses
        message = str(payload.get("message") or "")
        if not message:
            message = f"任务状态：{status or 'unknown'}"
        return TaskExecutionResult(succeeded, message, payload)


__all__ = [
    "CancellationProvider",
    "CustomOrderOperationsFactory",
    "CustomOrderStatusCheck",
    "DesktopTaskRunner",
    "NotificationContactRefreshCallable",
    "TaskExecutionResult",
    "RuntimeWriteGuardProvider",
]
