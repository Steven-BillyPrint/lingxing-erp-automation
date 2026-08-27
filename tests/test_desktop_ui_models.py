from __future__ import annotations

import importlib

import pytest

from erp_automation import app
from erp_automation.ui import (
    Capability,
    CapabilityMode,
    CapabilityPolicy,
    DashboardMetrics,
    DesktopSettings,
    InMemoryBackgroundTaskController,
    LINGXING_BROWSER_LOGIN_TRIGGER,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
    PYSIDE6_AVAILABLE,
    PySide6RequiredError,
    TaskArea,
    TaskCommand,
    TaskRecord,
    TaskStatus,
)
from erp_automation.ui.models import task_requires_visible_browser


def test_ui_models_import_without_pyside6() -> None:
    models = importlib.import_module("erp_automation.ui.models")
    controller = importlib.import_module("erp_automation.ui.controller")

    assert models.CapabilityMode.API_FIRST.label == "API 优先"
    assert controller.InMemoryBackgroundTaskController is not None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("api", CapabilityMode.API_FIRST),
        ("API优先", CapabilityMode.API_FIRST),
        ("网页", CapabilityMode.BROWSER),
        ("off", CapabilityMode.DISABLED),
    ],
)
def test_capability_mode_accepts_user_facing_aliases(value: str, expected: CapabilityMode) -> None:
    assert CapabilityMode.coerce(value) is expected


def test_capability_policy_applies_emergency_stop_only_to_erp_writes() -> None:
    policy = CapabilityPolicy(emergency_stop_writes=True)

    assert Capability.UPDATE_CONTACT.default_mode is CapabilityMode.BROWSER
    assert policy.effective_mode_for(Capability.LIST_ORDERS) is CapabilityMode.API_FIRST
    assert policy.effective_mode_for(Capability.DOWNLOAD_CUSTOM_ZIP) is CapabilityMode.API_FIRST
    assert policy.effective_mode_for(Capability.ALIBABA_LOGISTICS) is CapabilityMode.BROWSER
    assert policy.effective_mode_for(Capability.ALIBABA_ORDER_PREPARE) is CapabilityMode.BROWSER
    assert policy.effective_mode_for(Capability.ALIBABA_ORDER_DRAFT) is CapabilityMode.DISABLED
    assert policy.effective_mode_for(Capability.UPDATE_CONTACT) is CapabilityMode.DISABLED
    assert policy.effective_mode_for(Capability.OUTBOUND_ORDER) is CapabilityMode.DISABLED

    policy.emergency_stop_writes = False
    policy.set_mode(Capability.UPDATE_CONTACT, "网页")
    assert policy.configured_mode_for(Capability.UPDATE_CONTACT) is CapabilityMode.BROWSER
    assert policy.effective_mode_for(Capability.UPDATE_CONTACT) is CapabilityMode.BROWSER


def test_api_shipment_tasks_do_not_require_visible_browser() -> None:
    assert task_requires_visible_browser(
        TaskCommand(
            "执行自动标发",
            TaskArea.SHIPMENT,
            Capability.OUTBOUND_ORDER,
        )
    ) is False
    assert task_requires_visible_browser(
        TaskCommand(
            "同步客户通知",
            TaskArea.SHIPMENT,
            Capability.LIST_ORDERS,
            payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
        )
    ) is False
    assert task_requires_visible_browser(
        TaskCommand(
            "获取阿里巴巴物流",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_LOGISTICS,
        )
    ) is True
    assert task_requires_visible_browser(
        TaskCommand(
            "准备阿里物流下单",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_ORDER_PREPARE,
        )
    ) is True
    assert task_requires_visible_browser(
        TaskCommand(
            "填写阿里物流草稿",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_ORDER_DRAFT,
        )
    ) is True


def test_lingxing_login_task_requires_the_submitting_desktop_browser() -> None:
    assert task_requires_visible_browser(
        TaskCommand(
            "登录当前电脑的领星账号",
            TaskArea.MAINTENANCE,
            Capability.LIST_ORDERS,
            payload={"trigger": LINGXING_BROWSER_LOGIN_TRIGGER},
        )
    ) is True
    assert task_requires_visible_browser(
        TaskCommand(
            "测试领星 API",
            TaskArea.MAINTENANCE,
            Capability.LIST_ORDERS,
        )
    ) is False


def test_dashboard_metrics_groups_failed_and_blocked_as_attention() -> None:
    tasks = [
        TaskRecord("1", "queued", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS),
        TaskRecord("2", "running", TaskArea.SHIPMENT, Capability.LIST_ORDERS, status=TaskStatus.RUNNING),
        TaskRecord("3", "done", TaskArea.SHIPMENT, Capability.OUTBOUND_ORDER, status=TaskStatus.SUCCEEDED),
        TaskRecord("4", "failed", TaskArea.SHIPMENT, Capability.OUTBOUND_ORDER, status=TaskStatus.FAILED),
        TaskRecord("5", "blocked", TaskArea.SHIPMENT, Capability.OUTBOUND_ORDER, status=TaskStatus.BLOCKED),
        TaskRecord("6", "cancelled", TaskArea.SHIPMENT, Capability.LIST_ORDERS, status=TaskStatus.CANCELLED),
    ]

    metrics = DashboardMetrics.from_tasks(tasks)

    assert metrics.queued == 1
    assert metrics.running == 1
    assert metrics.succeeded == 1
    assert metrics.attention == 2
    assert metrics.cancelled == 1


def test_in_memory_controller_is_safe_by_default_and_returns_snapshot_copies() -> None:
    controller = InMemoryBackgroundTaskController()
    blocked = controller.submit_task(
        TaskCommand(
            name="执行标发",
            area=TaskArea.SHIPMENT,
            capability=Capability.OUTBOUND_ORDER,
        )
    )
    accepted = controller.submit_task(
        TaskCommand(
            name="扫描候选",
            area=TaskArea.SHIPMENT,
            capability=Capability.LIST_ORDERS,
        )
    )

    assert not blocked.accepted
    assert "禁用" in blocked.message
    assert accepted.accepted
    assert accepted.task_id

    first = controller.snapshot()
    first.tasks.clear()
    second = controller.snapshot()
    assert len(second.tasks) == 1


def test_notification_review_rescan_cannot_be_queued_twice() -> None:
    controller = InMemoryBackgroundTaskController()
    command = TaskCommand(
        name="重新同步客户通知物流",
        area=TaskArea.SHIPMENT,
        capability=Capability.LIST_ORDERS,
        payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
    )

    first = controller.submit_task(command)
    duplicate = controller.submit_task(command)

    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.task_id == first.task_id
    assert "正在同步" in duplicate.message
    assert len(controller.snapshot().tasks) == 1


def test_controller_cancel_and_retry_respect_state_and_capability_policy() -> None:
    controller = InMemoryBackgroundTaskController()
    submitted = controller.submit_task(
        TaskCommand("扫描候选", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS)
    )
    assert submitted.task_id

    assert controller.cancel_task(submitted.task_id).accepted
    assert controller.snapshot().tasks[0].status is TaskStatus.CANCELLED
    assert controller.retry_task(submitted.task_id).accepted
    assert controller.snapshot().tasks[0].status is TaskStatus.QUEUED


def test_settings_validation_and_migration_entry_are_exposed() -> None:
    invalid = DesktopSettings(folder_root="", queue_path="", api_timeout_seconds=0)
    assert len(invalid.validate()) == 3

    controller = InMemoryBackgroundTaskController()
    rejected = controller.save_settings(invalid)
    checked = controller.run_migrations(dry_run=True)

    assert not rejected.accepted
    assert checked.accepted
    assert "预检" in controller.snapshot().migration.last_result


def test_app_returns_clear_error_when_pyside6_cannot_load(monkeypatch, capsys) -> None:
    def fail() -> None:
        raise PySide6RequiredError("测试环境没有 PySide6")

    monkeypatch.setattr(app, "require_pyside6", fail)

    assert app.main([]) == 2
    assert "桌面程序启动失败" in capsys.readouterr().err


def test_qt_module_can_be_imported_in_headless_environment() -> None:
    qt_module = importlib.import_module("erp_automation.ui.qt")
    assert qt_module.DesktopMainWindow is not None

    if not PYSIDE6_AVAILABLE:
        with pytest.raises(PySide6RequiredError, match="PySide6"):
            qt_module.DesktopMainWindow(None)
