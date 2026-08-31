from __future__ import annotations

import os
from uuid import uuid4

import pytest

import erp_automation.app as desktop_app
import erp_automation.crash_diagnostics as crash_diagnostics_module
import erp_automation.single_instance as single_instance_module
from erp_automation.crash_diagnostics import CrashDiagnostics
from erp_automation.single_instance import acquire_desktop_single_instance
from shipment_automation.notification_queue import (
    STATUS_AWAITING_REVIEW_SUPPLEMENTAL,
    STATUS_FAILED_STATUS_CHECK,
    STATUS_FAILED_UNSENT,
    notification_queue_priority,
    notification_queue_status_key,
    notification_queue_status_label,
    ordered_notification_status_keys,
)


def test_notification_queue_status_names_and_priority_are_centralized() -> None:
    assert notification_queue_status_key(
        "AWAITING_REVIEW", 0, True, ""
    ) == STATUS_AWAITING_REVIEW_SUPPLEMENTAL
    assert notification_queue_status_key(
        "FAILED", 0, False, "状态核验超时：network"
    ) == STATUS_FAILED_STATUS_CHECK
    assert notification_queue_status_key(
        "FAILED", 0, False, "provider rejected"
    ) == STATUS_FAILED_UNSENT
    assert notification_queue_status_label(STATUS_FAILED_STATUS_CHECK) == (
        "发送结果待核验"
    )
    assert notification_queue_status_label(STATUS_FAILED_UNSENT) == (
        "发送未成功，需人工处理"
    )
    assert notification_queue_priority("SENDING") < notification_queue_priority(
        "AWAITING_REVIEW"
    )
    assert notification_queue_priority("AWAITING_REVIEW") < (
        notification_queue_priority("DELIVERED")
    )
    assert ordered_notification_status_keys(
        ["DELIVERED", "SENDING", STATUS_FAILED_UNSENT]
    ) == ("SENDING", STATUS_FAILED_UNSENT, "DELIVERED")


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex behavior")
def test_desktop_single_instance_mutex_is_released_cleanly() -> None:
    name = rf"Local\ERPAutomation.Test.{uuid4().hex}"
    first = acquire_desktop_single_instance(name)
    second = acquire_desktop_single_instance(name)
    try:
        assert first.acquired is True
        assert second.acquired is False
    finally:
        second.close()
        first.close()

    replacement = acquire_desktop_single_instance(name)
    try:
        assert replacement.acquired is True
    finally:
        replacement.close()


def test_crash_diagnostics_records_exception_without_business_context(tmp_path) -> None:
    path = tmp_path / "client-crash.log"
    diagnostics = CrashDiagnostics(path).install()
    try:
        try:
            raise RuntimeError("diagnostic sentinel")
        except RuntimeError as exc:
            diagnostics._write_exception(
                "test_exception",
                type(exc),
                exc,
                exc.__traceback__,
            )
    finally:
        diagnostics.close()

    content = path.read_text(encoding="utf-8")
    assert "client_start" in content
    assert "test_exception" in content
    assert "exception_type=builtins.RuntimeError" in content
    assert "diagnostic sentinel" not in content
    assert "client_stop" in content


def test_packaged_second_launch_activates_existing_window_and_exits(
    monkeypatch,
) -> None:
    events: list[str] = []
    guard = type("Guard", (), {"acquired": False})()
    monkeypatch.setattr(desktop_app, "require_pyside6", lambda: None)
    monkeypatch.setattr(desktop_app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(desktop_app, "is_local_test_mode", lambda: False)
    monkeypatch.setattr(
        crash_diagnostics_module,
        "install_crash_diagnostics",
        lambda: events.append("diagnostics"),
    )
    monkeypatch.setattr(
        single_instance_module,
        "acquire_desktop_single_instance",
        lambda: guard,
    )
    monkeypatch.setattr(
        single_instance_module,
        "activate_existing_desktop_window",
        lambda: events.append("activate") or True,
    )
    monkeypatch.setattr(
        desktop_app,
        "should_bootstrap_packaged_shared_client",
        lambda: pytest.fail("duplicate launch must exit before bootstrap"),
    )

    assert desktop_app.main([]) == 0
    assert events == ["activate"]
