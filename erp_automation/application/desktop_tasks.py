"""In-process business task runner used by the desktop application."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from erp_automation.ui.models import (
    Capability,
    DesktopSettings,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    TaskArea,
    TaskCommand,
)
from lingxing_automation.services.custom_order_api import CustomOrderApiOperations


ScanCallable = Callable[
    [DesktopSettings, Mapping[str, Any], str | None],
    Awaitable[Mapping[str, Any]],
]
ErpMarkCallable = Callable[..., Awaitable[str]]
CustomOrderOperationsFactory = Callable[
    [DesktopSettings, Mapping[str, Any]],
    AbstractAsyncContextManager[CustomOrderApiOperations],
]
RuntimeWriteGuardProvider = Callable[[], bool]


@dataclass(frozen=True)
class TaskExecutionResult:
    succeeded: bool
    message: str
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    blocked: bool = False


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
        api_test: ScanCallable | None = None,
        erp_mark_func: ErpMarkCallable | None = None,
        custom_order_api_factory: CustomOrderOperationsFactory | None = None,
        runtime_write_guard_provider: RuntimeWriteGuardProvider | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.settings_provider = settings_provider
        self.configuration_provider = configuration_provider
        self.custom_scan = custom_scan
        self.shipment_scan = shipment_scan
        self.api_test = api_test
        self.erp_mark_func = erp_mark_func
        self.custom_order_api_factory = custom_order_api_factory
        self.runtime_write_guard_provider = runtime_write_guard_provider
        self._consumed_confirmation_ids: set[str] = set()

    def __call__(self, command: TaskCommand) -> TaskExecutionResult:
        return asyncio.run(self.run(command))

    async def run(self, command: TaskCommand) -> TaskExecutionResult:
        settings = self.settings_provider()
        configuration = dict(self.configuration_provider())
        if command.area is TaskArea.MAINTENANCE and command.capability is Capability.LIST_ORDERS:
            if self.api_test is None:
                return TaskExecutionResult(False, "领星 API 连接测试器尚未连接。")
            payload = dict(await self.api_test(settings, configuration, command.execution_id))
            return self._result(payload, success_statuses={"completed"})
        if command.area is TaskArea.CUSTOMIZATION and command.capability is Capability.LIST_ORDERS:
            if self.custom_scan is None:
                return TaskExecutionResult(False, "API 定制订单扫描器尚未连接。")
            payload = dict(await self.custom_scan(settings, configuration, command.execution_id))
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
            )
        if command.area is TaskArea.SHIPMENT and command.capability is Capability.LIST_ORDERS:
            if self.shipment_scan is None:
                return TaskExecutionResult(False, "API 自动标发扫描器尚未连接。")
            payload = dict(await self.shipment_scan(settings, configuration, command.execution_id))
            return self._result(payload, success_statuses={"completed"})
        if command.area is TaskArea.SHIPMENT and command.capability is Capability.ALIBABA_LOGISTICS:
            return await self._query_logistics(settings, configuration)
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
            )
        return TaskExecutionResult(False, f"当前桌面任务没有执行器：{command.name}")

    def _common_browser_args(self, args: argparse.Namespace, settings: DesktopSettings) -> None:
        args.configuration_values = dict(self.configuration_provider())
        args.profile_dir = str(self._path(settings.browser_profile))
        args.log_dir = str(self._path(settings.log_dir))
        args.debug_log_dir = str(self._path(Path("debug") / "logs"))
        args.keep_browser_open = False
        args.headless = False
        args.no_auto_login = False

    async def _process_custom_order(
        self,
        platform_order_no: str,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        confirmation: DesktopWriteConfirmation,
    ) -> TaskExecutionResult:
        from lingxing_automation.cli import build_parser
        from lingxing_automation.flows.contact_sync import (
            CustomOrderInteractionPolicy,
            run_retry_order,
        )

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
        self._common_browser_args(args, settings)
        args.configuration_values = dict(configuration)
        # Unlike the command-line safe-retry preset, a confirmed desktop
        # "process" action is the normal production workflow.
        args.no_dedupe_write = False
        args.no_create_folder = False
        args.resume_workflow_stages = True
        confirmed_steps: list[str] = []

        async def confirm_writeback(context: dict[str, Any]) -> bool:
            expected_platform = str(context.get("expected_platform_order_no") or "").strip()
            expected_system = str(context.get("expected_system_order_no") or "").strip()
            if expected_platform != confirmation.order_no:
                return False
            if confirmation.system_order_no and expected_system != confirmation.system_order_no:
                return False
            confirmed_steps.append("contact_writeback")
            return True

        async def confirm_folder(platform: str, system: str, _result: Any) -> bool:
            if platform != confirmation.order_no:
                return False
            if confirmation.system_order_no and system != confirmation.system_order_no:
                return False
            confirmed_steps.append("folder_creation")
            return True

        async def confirm_plan(plan: Any) -> bool:
            if str(getattr(plan, "platform_order_no", "")) != confirmation.order_no:
                return False
            confirmed_steps.append("automated_plan")
            return True

        async def reject_manual_sku(_platform: str, _system: str, _reason: str | None) -> bool:
            confirmed_steps.append("manual_sku_requires_review")
            return False

        async def reject_manual_split(_plan: Any) -> bool:
            confirmed_steps.append("manual_split_requires_review")
            return False

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
            if len(unique) == 1:
                confirmed_steps.append("single_contact_selected")
                return unique[0]
            confirmed_steps.append("ambiguous_contact_requires_review")
            return None

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

        args.custom_order_interaction_policy = CustomOrderInteractionPolicy(
            confirm_writeback=confirm_writeback,
            confirm_folder_creation=confirm_folder,
            confirm_sku_plan=confirm_plan,
            confirm_manual_sku_done=reject_manual_sku,
            confirm_package_split_plan=confirm_plan,
            confirm_manual_package_split_done=reject_manual_split,
            choose_contact=choose_contact,
            runtime_write_guard=runtime_write_guard,
        )
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
        payload["desktop_confirmation_id"] = confirmation.confirmation_id
        payload["desktop_confirmed_steps"] = list(confirmed_steps)
        return self._custom_order_result(
            payload,
            platform_order_no=platform_order_no,
            settings=settings,
        )

    async def _query_logistics(
        self,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
    ) -> TaskExecutionResult:
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
        self._common_browser_args(args, settings)
        args.configuration_values = dict(configuration)
        payload = dict(await run_logistics_worker(args))
        return self._result(payload, success_statuses={"completed", "completed_with_skips"})

    async def _mark_shipment(
        self,
        logistics_no: str,
        settings: DesktopSettings,
        configuration: Mapping[str, Any],
        confirmation: DesktopWriteConfirmation,
    ) -> TaskExecutionResult:
        from shipment_automation.cli import build_parser
        from shipment_automation.erp_mark_ship import run_erp_mark_worker

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
        self._common_browser_args(args, settings)
        args.configuration_values = dict(configuration)
        args.logistics_no = logistics_no
        confirmation_hashes: list[str] = []

        async def desktop_confirm(prompt: str) -> bool:
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            confirmation_hashes.append(prompt_hash)
            return True

        # The queue worker reads these attributes and records one audit event
        # for every approved dangerous-write boundary without storing the
        # potentially sensitive prompt text itself.
        desktop_confirm.confirmation_id = confirmation.confirmation_id  # type: ignore[attr-defined]
        desktop_confirm.confirmation_source = confirmation.source  # type: ignore[attr-defined]
        args.confirm_func = desktop_confirm
        if self.erp_mark_func is not None:
            args.mark_item_func = self.erp_mark_func
        payload = dict(await run_erp_mark_worker(args))
        payload["desktop_confirmation_id"] = confirmation.confirmation_id
        payload["desktop_confirmed_prompt_hashes"] = confirmation_hashes
        result = self._result(payload, success_statuses={"completed", "completed_with_skips"})
        if payload.get("blocked_count") or payload.get("manual_review_count"):
            return TaskExecutionResult(False, result.message, payload, blocked=True)
        return result

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

    def _custom_order_result(
        self,
        payload: dict[str, Any],
        *,
        platform_order_no: str,
        settings: DesktopSettings,
    ) -> TaskExecutionResult:
        items = [item for item in payload.get("items") or [] if isinstance(item, Mapping)]
        item = dict(items[0]) if len(items) == 1 else {}
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
                f"订单={item_status or 'missing'}），已停止并转人工复核。"
            )
        payload["original_status"] = original_status
        payload["status"] = "blocked"
        payload["manual_review_required"] = True
        payload["message"] = message
        stage = self._blocked_custom_stage(item)
        payload["workflow_blocked_stage"] = stage
        self._record_custom_workflow_blocked(
            settings,
            platform_order_no,
            stage=stage,
            reason=message,
            result_status=item_status or original_status,
            payload=payload,
        )
        return TaskExecutionResult(False, message, payload, blocked=True)

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
    def _blocked_custom_stage(item: Mapping[str, Any]) -> str:
        if item.get("instruction_remark_status") or item.get("instruction_remark_error"):
            return "instruction_remark"
        if item.get("package_split_status") or item.get("package_split_error"):
            return "package_split"
        if item.get("sku_adjustment_status") or item.get("sku_adjustment_error"):
            return "sku"
        if item.get("folder_status") or item.get("folder_error"):
            return "folder"
        return "contact"

    def _record_custom_workflow_blocked(
        self,
        settings: DesktopSettings,
        platform_order_no: str,
        *,
        stage: str,
        reason: str,
        result_status: str,
        payload: dict[str, Any],
    ) -> None:
        state_path = self._path(settings.custom_state_path)
        if state_path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
            payload["workflow_block_recorded"] = False
            return
        try:
            from erp_automation.persistence import CustomWorkflowStore, WorkflowStageState

            store = CustomWorkflowStore(state_path)
            if store.get_workflow(platform_order_no) is None:
                store.mutate_legacy_record(
                    platform_order_no,
                    lambda current: {**current, "workflow_status": "pending"},
                    event_type="desktop_processing_blocked_initialized",
                    actor="desktop_worker",
                    reason=reason,
                )
            store.set_stage_state(
                platform_order_no,
                stage,
                WorkflowStageState.BLOCKED,
                reason=reason,
                actor="desktop_worker",
                result_status=result_status,
                last_error=reason,
            )
            payload["workflow_block_recorded"] = True
        except Exception as exc:
            payload["workflow_block_recorded"] = False
            payload["workflow_block_error_type"] = type(exc).__name__

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
    "CustomOrderOperationsFactory",
    "DesktopTaskRunner",
    "TaskExecutionResult",
    "RuntimeWriteGuardProvider",
]
