from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextEdit,
)

from erp_automation import app as app_module
from erp_automation.ui import modern_dialogs


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def test_packaged_startup_dialog_has_modern_status_hierarchy(qt_app) -> None:
    dialog, status = modern_dialogs.build_packaged_startup_dialog()
    try:
        assert dialog.objectName() == "erpModernDialog"
        assert dialog.size().width() == 520
        assert status.objectName() == "statusText"
        assert status.text() == "正在准备启动…"
        assert dialog.findChild(QLabel, "brandBadge").text() == "ERP"
        progress = dialog.findChild(QProgressBar, "startupProgress")
        assert progress.minimum() == 0
        assert progress.maximum() == 0
        assert "#2F6FED" in dialog.styleSheet()
        assert "border-radius: 18px" in dialog.styleSheet()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_packaged_startup_feedback_only_shows_for_slow_startup(qt_app) -> None:
    dialog, status = modern_dialogs.build_packaged_startup_dialog()
    now = [100.0]
    feedback = app_module._PackagedStartupFeedback(
        qt_app,
        dialog,
        status,
        owns_application=False,
        show_delay_seconds=0.75,
        clock=lambda: now[0],
    )
    try:
        assert dialog.isVisible() is False

        feedback.update("正在检查客户端更新…")
        assert status.text() == "正在检查客户端更新…"
        assert dialog.isVisible() is False

        now[0] += 0.74
        feedback.update("正在连接阿里云共享服务…")
        assert dialog.isVisible() is False

        now[0] += 0.02
        feedback.update("正在连接阿里云共享服务…")
        assert dialog.isVisible() is True
        assert dialog.objectName() == "erpModernDialog"
    finally:
        feedback.close()


def test_packaged_startup_feedback_factory_does_not_flash_immediately(qt_app) -> None:
    feedback = app_module.create_packaged_startup_feedback([])
    try:
        assert feedback.window.isVisible() is False
        assert (
            feedback._show_delay_seconds
            == app_module.PACKAGED_STARTUP_DIALOG_DELAY_SECONDS
        )
    finally:
        feedback.close()


def test_packaged_client_error_uses_modern_selectable_dialog(
    qt_app,
    monkeypatch,
) -> None:
    captured: dict[str, QDialog] = {}

    def fake_exec(dialog: QDialog):
        captured["dialog"] = dialog
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    modern_dialogs.show_packaged_client_error_dialog(
        "更新失败，已保留原版本。",
    )

    dialog = captured["dialog"]
    assert dialog.objectName() == "erpModernDialog"
    assert dialog.findChild(QLabel, "dialogTitle").text() == "客户端未能启动"
    details = dialog.findChild(QTextEdit, "diagnosticText")
    assert details.isReadOnly()
    assert details.toPlainText() == "更新失败，已保留原版本。"
    assert dialog.findChild(QPushButton, "dangerButton").text() == "关闭"
    dialog.deleteLater()


def test_app_packaged_error_delegates_to_modern_dialog(
    qt_app,
    monkeypatch,
) -> None:
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        modern_dialogs,
        "show_packaged_client_error_dialog",
        lambda message, *, parent=None, window_title="ERP 自动化": captured.append(
            (message, window_title)
        ),
    )

    app_module.show_packaged_client_error(RuntimeError("下载校验失败"))

    assert len(captured) == 1
    assert "下载校验失败" in captured[0][0]
    assert "请重新安装最新版客户端" in captured[0][0]
    assert captured[0][1] == "ERP 自动化"


def test_candidate_packaged_error_keeps_candidate_identity(
    qt_app,
    monkeypatch,
) -> None:
    captured: list[tuple[str, str]] = []
    monkeypatch.setenv("ERP_AUTOMATION_CLIENT_PROFILE", "candidate")
    monkeypatch.setenv(
        "ERP_AUTOMATION_CLIENT_DISPLAY_VERSION",
        "2026.08.06.2-candidate.1",
    )
    monkeypatch.setattr(
        modern_dialogs,
        "show_packaged_client_error_dialog",
        lambda message, *, parent=None, window_title="ERP 自动化": captured.append(
            (message, window_title)
        ),
    )

    app_module.show_packaged_client_error(RuntimeError("服务器版本尚未晋级"))

    assert captured == [
        (
            "无法启动候选客户端。\n\n服务器版本尚未晋级\n\n"
            "请重新安装最新版客户端，或联系管理员检查客户端凭据。",
            "ERP 自动化候选版 2026.08.06.2-candidate.1",
        )
    ]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (QDialog.DialogCode.Accepted, True),
        (QDialog.DialogCode.Rejected, False),
    ],
)
def test_cloudflare_login_dialog_requires_explicit_choice(
    qt_app,
    monkeypatch,
    result,
    expected,
) -> None:
    captured: dict[str, QDialog] = {}

    def fake_exec(dialog: QDialog):
        captured["dialog"] = dialog
        return result

    monkeypatch.setattr(QDialog, "exec", fake_exec)

    assert (
        modern_dialogs.confirm_cloudflare_access_login(
            "会话已过期。",
        )
        is expected
    )
    dialog = captured["dialog"]
    assert dialog.objectName() == "erpModernDialog"
    assert dialog.findChild(QLabel, "brandBadge").text() == "SSO"
    assert dialog.findChild(QPushButton, "primaryButton").text() == "打开网页登录"
    assert dialog.findChild(QPushButton, "secondaryButton").text() == "稍后再说"
    assert "会话已过期。" in [
        label.text() for label in dialog.findChildren(QLabel)
    ]
    dialog.deleteLater()


def test_app_login_prompt_delegates_to_modern_dialog(qt_app, monkeypatch) -> None:
    captured: list[tuple[str, object]] = []
    parent = object()
    monkeypatch.setattr(
        modern_dialogs,
        "confirm_cloudflare_access_login",
        lambda reason, *, parent=None: captured.append((reason, parent)) or True,
    )

    assert app_module.prompt_cloudflare_access_login(
        "需要重新认证。",
        parent=parent,
    )
    assert captured == [("需要重新认证。", parent)]
