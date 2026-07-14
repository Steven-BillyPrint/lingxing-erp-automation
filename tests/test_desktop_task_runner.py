from __future__ import annotations

import asyncio
import builtins
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from erp_automation.application.desktop_tasks import DesktopTaskRunner
from erp_automation.persistence import CustomWorkflowStore, WorkflowStageState
from erp_automation.ui.models import (
    Capability,
    DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    DesktopSettings,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    TaskArea,
    TaskCommand,
)
from lingxing_automation.flows import contact_sync
from lingxing_automation.models import ContactInfo
from shipment_automation import erp_mark_ship


PLATFORM_ORDER_NO = "111-2222222-3333333"
SYSTEM_ORDER_NO = "103000000000000001"


def _settings(tmp_path) -> DesktopSettings:
    return DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path=str(tmp_path / "state.sqlite3"),
        queue_path=str(tmp_path / "shipment.sqlite3"),
        browser_profile=str(tmp_path / "browser"),
        log_dir=str(tmp_path / "logs"),
    )


def _custom_command(
    confirmation: DesktopWriteConfirmation | None = None,
) -> TaskCommand:
    confirmation = confirmation or DesktopWriteConfirmation.create(
        DesktopWriteAction.PROCESS_CUSTOM_ORDER,
        PLATFORM_ORDER_NO,
    )
    return TaskCommand(
        name="process",
        area=TaskArea.CUSTOMIZATION,
        capability=Capability.UPDATE_CONTACT,
        order_no=PLATFORM_ORDER_NO,
        payload={DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload()},
    )


def test_scan_callables_receive_command_execution_id(tmp_path) -> None:
    observed: list[tuple[TaskArea, str | None]] = []

    def scanner_for(area: TaskArea):
        async def scan(
            _settings: DesktopSettings,
            _configuration: dict[str, Any],
            execution_id: str | None,
        ) -> dict[str, Any]:
            observed.append((area, execution_id))
            return {"status": "completed", "message": "scan complete"}

        return scan

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        api_test=scanner_for(TaskArea.MAINTENANCE),
        custom_scan=scanner_for(TaskArea.CUSTOMIZATION),
        shipment_scan=scanner_for(TaskArea.SHIPMENT),
    )

    for area in (
        TaskArea.MAINTENANCE,
        TaskArea.CUSTOMIZATION,
        TaskArea.SHIPMENT,
    ):
        execution_id = f"task-{area.value}"
        result = runner(
            TaskCommand(
                name=f"scan-{area.value}",
                area=area,
                capability=Capability.LIST_ORDERS,
                execution_id=execution_id,
            )
        )
        assert result.succeeded is True

    assert observed == [
        (TaskArea.MAINTENANCE, "task-maintenance"),
        (TaskArea.CUSTOMIZATION, "task-customization"),
        (TaskArea.SHIPMENT, "task-shipment"),
    ]


def test_custom_order_factory_is_created_and_closed_inside_each_task_loop(monkeypatch, tmp_path) -> None:
    events: list[tuple[str, int, object]] = []

    @asynccontextmanager
    async def factory(_settings: DesktopSettings, _configuration: dict[str, Any]):
        operations = object()
        loop_id = id(asyncio.get_running_loop())
        events.append(("enter", loop_id, operations))
        try:
            yield operations
        finally:
            assert id(asyncio.get_running_loop()) == loop_id
            events.append(("exit", loop_id, operations))

    async def fake_retry(args):
        current = events[-1]
        assert current[0] == "enter"
        assert args.custom_order_api_operations is current[2]
        assert args.resume_workflow_stages is True
        assert args.custom_order_interaction_policy is not None
        assert id(asyncio.get_running_loop()) == current[1]
        return {
            "status": "completed",
            "updated_count": 1,
            "message": "ok",
            "items": [{"status": "updated", "message": "ok"}],
        }

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    settings = _settings(tmp_path)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        custom_order_api_factory=factory,
    )
    first = runner(_custom_command())
    second = runner(_custom_command())

    assert first.succeeded is True
    assert second.succeeded is True
    assert [event[0] for event in events] == ["enter", "exit", "enter", "exit"]
    assert events[0][2] is events[1][2]
    assert events[2][2] is events[3][2]
    assert events[0][2] is not events[2][2]


def test_custom_desktop_interactions_never_read_stdin_and_guard_is_dynamic(
    monkeypatch,
    tmp_path,
) -> None:
    guard_state = {"enabled": True}

    def forbidden_input(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("packaged desktop task must never read stdin")

    monkeypatch.setattr(builtins, "input", forbidden_input)

    async def fake_retry(args):
        policy = args.custom_order_interaction_policy
        assert await policy.confirm_writeback(
            {
                "expected_platform_order_no": PLATFORM_ORDER_NO,
                "expected_system_order_no": SYSTEM_ORDER_NO,
            }
        )
        assert await policy.confirm_folder_creation(PLATFORM_ORDER_NO, SYSTEM_ORDER_NO, object())
        plan = SimpleNamespace(platform_order_no=PLATFORM_ORDER_NO)
        assert await policy.confirm_sku_plan(plan)
        assert await policy.confirm_package_split_plan(plan)
        assert not await policy.confirm_manual_sku_done(PLATFORM_ORDER_NO, SYSTEM_ORDER_NO, "manual")
        assert not await policy.confirm_manual_package_split_done(plan)
        contact = ContactInfo(
            phone="15551234567",
            email="buyer@example.com",
            source_count=1,
            source_excerpt="test",
        )
        assert await policy.choose_contact(PLATFORM_ORDER_NO, SYSTEM_ORDER_NO, [contact]) is contact
        assert await policy.runtime_write_guard("contact_email", PLATFORM_ORDER_NO, SYSTEM_ORDER_NO)
        guard_state["enabled"] = False
        assert not await policy.runtime_write_guard("folder_create", PLATFORM_ORDER_NO, SYSTEM_ORDER_NO)
        return {
            "status": "completed",
            "updated_count": 1,
            "items": [{"status": "updated"}],
        }

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: guard_state["enabled"],
    )

    result = runner(_custom_command())

    assert result.succeeded is True
    assert result.payload["desktop_confirmed_steps"][-2:] == [
        "write_guard_allowed:contact_email",
        "write_guard_blocked:folder_create",
    ]


def test_write_confirmation_is_required_and_cannot_be_reused(monkeypatch, tmp_path) -> None:
    calls = 0

    async def fake_retry(_args):
        nonlocal calls
        calls += 1
        return {
            "status": "completed",
            "updated_count": 1,
            "items": [{"status": "updated"}],
        }

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: True,
    )
    missing = TaskCommand(
        name="process",
        area=TaskArea.CUSTOMIZATION,
        capability=Capability.UPDATE_CONTACT,
        order_no=PLATFORM_ORDER_NO,
    )
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.PROCESS_CUSTOM_ORDER,
        PLATFORM_ORDER_NO,
    )
    command = _custom_command(confirmation)

    assert runner(missing).blocked is True
    assert runner(command).succeeded is True
    replay = runner(command)

    assert replay.blocked is True
    assert "已经使用" in replay.message
    assert calls == 1


def test_nested_manual_result_blocks_and_persists_failed_stage(monkeypatch, tmp_path) -> None:
    async def fake_retry(_args):
        return {
            "status": "completed",
            "updated_count": 1,
            "items": [
                {
                    "status": "updated_folder_created_sku_failed",
                    "sku_adjustment_status": "sku_adjustment_manual_review",
                    "sku_adjustment_error": "API 结果不明确，禁止重复写入。",
                    "message": "API 结果不明确，禁止重复写入。",
                }
            ],
        }

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    settings = _settings(tmp_path)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: True,
    )

    result = runner(_custom_command())

    assert result.succeeded is False
    assert result.blocked is True
    assert result.payload["status"] == "blocked"
    assert result.payload["workflow_blocked_stage"] == "sku"
    assert result.payload["workflow_block_recorded"] is True
    workflow = CustomWorkflowStore(settings.custom_state_path).get_workflow(PLATFORM_ORDER_NO)
    assert workflow is not None
    sku = next(stage for stage in workflow["stages"] if stage["stage"] == "sku")
    assert sku["state"] == str(WorkflowStageState.BLOCKED)
    assert sku["last_error"] == "API 结果不明确，禁止重复写入。"


def test_erp_desktop_confirmation_callback_never_reads_stdin(monkeypatch, tmp_path) -> None:
    def forbidden_input(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("packaged desktop ERP worker must never read stdin")

    monkeypatch.setattr(builtins, "input", forbidden_input)
    observed: dict[str, Any] = {}

    async def fake_worker(args):
        observed["confirmation_id"] = args.confirm_func.confirmation_id
        observed["source"] = args.confirm_func.confirmation_source
        assert await args.confirm_func("dangerous write one") is True
        assert await args.confirm_func("dangerous write two") is True
        return {"status": "completed", "message": "ok"}

    monkeypatch.setattr(erp_mark_ship, "run_erp_mark_worker", fake_worker)
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.EXECUTE_ERP_MARK,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        logistics_no="LP123456789",
    )
    command = TaskCommand(
        name="mark",
        area=TaskArea.SHIPMENT,
        capability=Capability.OUTBOUND_ORDER,
        order_no=PLATFORM_ORDER_NO,
        payload={
            "system_order_no": SYSTEM_ORDER_NO,
            "logistics_no": "LP123456789",
            DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
        },
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
    )

    result = runner(command)

    assert result.succeeded is True
    assert observed == {
        "confirmation_id": confirmation.confirmation_id,
        "source": "qt_message_box",
    }
    assert len(result.payload["desktop_confirmed_prompt_hashes"]) == 2
