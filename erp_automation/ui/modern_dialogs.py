from __future__ import annotations

from typing import Any


_DIALOG_STYLE = """
QDialog#erpModernDialog {
    background: #F4F7FC;
}
QFrame#dialogCard {
    background: #FFFFFF;
    border: 1px solid #DCE5F2;
    border-radius: 18px;
}
QLabel#brandBadge {
    background: #E8F0FF;
    color: #245FCE;
    border: 1px solid #C8D9FA;
    border-radius: 12px;
    font-size: 13px;
    font-weight: 700;
}
QLabel#dialogTitle {
    color: #10213A;
    font-size: 20px;
    font-weight: 700;
}
QLabel#dialogSubtitle {
    color: #66758C;
    font-size: 12px;
}
QFrame#statusPanel {
    background: #F7F9FD;
    border: 1px solid #E3E9F3;
    border-radius: 12px;
}
QLabel#statusEyebrow {
    color: #2F6FED;
    font-size: 11px;
    font-weight: 700;
}
QLabel#statusText {
    color: #263A57;
    font-size: 13px;
}
QLabel#hintText {
    color: #7B879A;
    font-size: 11px;
}
QLabel#stepNumber {
    background: #E8F0FF;
    color: #245FCE;
    border-radius: 10px;
    font-weight: 700;
}
QLabel#stepText {
    color: #40516B;
    font-size: 12px;
}
QProgressBar {
    min-height: 8px;
    max-height: 8px;
    border: none;
    border-radius: 4px;
    background: #E7EDF7;
}
QProgressBar::chunk {
    border-radius: 4px;
    background: #2F6FED;
}
QPushButton {
    min-height: 38px;
    padding: 0 18px;
    border-radius: 9px;
    border: 1px solid #CFD9E8;
    background: #FFFFFF;
    color: #34445E;
    font-size: 12px;
    font-weight: 600;
}
QPushButton:hover {
    background: #F5F8FD;
    border-color: #AFC0D8;
}
QPushButton:pressed {
    background: #EAF0F8;
}
QPushButton#primaryButton {
    border-color: #2F6FED;
    background: #2F6FED;
    color: #FFFFFF;
}
QPushButton#primaryButton:hover {
    border-color: #245FCE;
    background: #245FCE;
}
QPushButton#primaryButton:pressed {
    border-color: #1E4FAE;
    background: #1E4FAE;
}
QPushButton#dangerButton {
    border-color: #D94A4A;
    background: #D94A4A;
    color: #FFFFFF;
}
QPushButton#dangerButton:hover {
    border-color: #BE3535;
    background: #BE3535;
}
QTextEdit#diagnosticText {
    border: 1px solid #E3E9F3;
    border-radius: 10px;
    background: #F7F9FD;
    color: #263A57;
    padding: 10px;
    font-family: "Microsoft YaHei UI";
    font-size: 12px;
}
"""


def _prepare_dialog(dialog: Any, *, title: str, width: int, height: int) -> None:
    from PySide6.QtCore import Qt

    dialog.setObjectName("erpModernDialog")
    dialog.setWindowTitle(title)
    dialog.setFixedSize(width, height)
    dialog.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
    dialog.setStyleSheet(_DIALOG_STYLE)


def build_packaged_startup_dialog() -> tuple[Any, Any]:
    """Build the non-blocking startup dialog and return its status label."""

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QProgressBar,
        QVBoxLayout,
    )

    dialog = QDialog()
    _prepare_dialog(dialog, title="ERP 自动化", width=520, height=286)
    dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(22, 22, 22, 22)

    card = QFrame()
    card.setObjectName("dialogCard")
    card_layout = QVBoxLayout(card)
    card_layout.setContentsMargins(24, 22, 24, 20)
    card_layout.setSpacing(16)

    heading = QHBoxLayout()
    heading.setSpacing(14)
    badge = QLabel("ERP")
    badge.setObjectName("brandBadge")
    badge.setFixedSize(48, 48)
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    heading.addWidget(badge)

    heading_text = QVBoxLayout()
    heading_text.setSpacing(3)
    title = QLabel("正在准备 ERP 自动化")
    title.setObjectName("dialogTitle")
    subtitle = QLabel("正在建立安全连接并加载工作区")
    subtitle.setObjectName("dialogSubtitle")
    heading_text.addWidget(title)
    heading_text.addWidget(subtitle)
    heading.addLayout(heading_text, 1)
    card_layout.addLayout(heading)

    status_panel = QFrame()
    status_panel.setObjectName("statusPanel")
    status_layout = QVBoxLayout(status_panel)
    status_layout.setContentsMargins(14, 11, 14, 11)
    status_layout.setSpacing(4)
    eyebrow = QLabel("启动进度")
    eyebrow.setObjectName("statusEyebrow")
    status_label = QLabel("正在准备启动…")
    status_label.setObjectName("statusText")
    status_label.setWordWrap(True)
    status_layout.addWidget(eyebrow)
    status_layout.addWidget(status_label)
    card_layout.addWidget(status_panel)

    progress = QProgressBar()
    progress.setObjectName("startupProgress")
    progress.setRange(0, 0)
    progress.setTextVisible(False)
    card_layout.addWidget(progress)

    hint = QLabel("程序就绪后此窗口会自动关闭")
    hint.setObjectName("hintText")
    hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
    card_layout.addWidget(hint)

    outer.addWidget(card)
    return dialog, status_label


def show_packaged_client_error_dialog(
    message: str,
    *,
    parent: Any = None,
) -> None:
    """Show a selectable, modern error card for every packaged startup failure."""

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QTextEdit,
        QVBoxLayout,
    )

    dialog = QDialog(parent)
    _prepare_dialog(
        dialog,
        title="ERP 自动化",
        width=620,
        height=430,
    )
    dialog.setModal(True)

    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(22, 22, 22, 22)

    card = QFrame()
    card.setObjectName("dialogCard")
    card_layout = QVBoxLayout(card)
    card_layout.setContentsMargins(24, 22, 24, 22)
    card_layout.setSpacing(16)

    heading = QHBoxLayout()
    heading.setSpacing(14)
    badge = QLabel("!")
    badge.setObjectName("brandBadge")
    badge.setFixedSize(48, 48)
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    heading.addWidget(badge)
    heading_text = QVBoxLayout()
    heading_text.setSpacing(3)
    title = QLabel("客户端未能启动")
    title.setObjectName("dialogTitle")
    subtitle = QLabel("本次操作尚未执行；如果发生在更新期间，原版本入口仍会保留。")
    subtitle.setObjectName("dialogSubtitle")
    subtitle.setWordWrap(True)
    heading_text.addWidget(title)
    heading_text.addWidget(subtitle)
    heading.addLayout(heading_text, 1)
    card_layout.addLayout(heading)

    details = QTextEdit()
    details.setObjectName("diagnosticText")
    details.setReadOnly(True)
    details.setPlainText(str(message or "发生未知错误。").strip())
    details.setMinimumHeight(190)
    card_layout.addWidget(details, 1)

    hint = QLabel("可复制诊断信息后交给管理员；请勿在公开渠道发送账号或密钥。")
    hint.setObjectName("hintText")
    hint.setWordWrap(True)
    card_layout.addWidget(hint)

    buttons = QHBoxLayout()
    buttons.setSpacing(10)
    copy_button = QPushButton("复制诊断信息")
    close_button = QPushButton("关闭")
    close_button.setObjectName("dangerButton")
    close_button.setDefault(True)
    copy_button.clicked.connect(
        lambda: QApplication.clipboard().setText(details.toPlainText())
    )
    close_button.clicked.connect(dialog.accept)
    buttons.addWidget(copy_button)
    buttons.addStretch(1)
    buttons.addWidget(close_button)
    card_layout.addLayout(buttons)

    outer.addWidget(card)
    dialog.exec()


def confirm_cloudflare_access_login(
    reason: str = "",
    *,
    parent: Any = None,
) -> bool:
    """Show an explicit, modern confirmation before opening web login."""

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
    )

    dialog = QDialog(parent)
    _prepare_dialog(
        dialog,
        title="需要企业邮箱登录",
        width=580,
        height=438,
    )
    dialog.setModal(True)

    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(22, 22, 22, 22)

    card = QFrame()
    card.setObjectName("dialogCard")
    card_layout = QVBoxLayout(card)
    card_layout.setContentsMargins(24, 22, 24, 22)
    card_layout.setSpacing(16)

    heading = QHBoxLayout()
    heading.setSpacing(14)
    badge = QLabel("SSO")
    badge.setObjectName("brandBadge")
    badge.setFixedSize(48, 48)
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    heading.addWidget(badge)
    heading_text = QVBoxLayout()
    heading_text.setSpacing(3)
    title = QLabel("需要重新验证企业邮箱")
    title.setObjectName("dialogTitle")
    subtitle = QLabel("当前登录会话不存在或已经过期，本次操作尚未执行")
    subtitle.setObjectName("dialogSubtitle")
    subtitle.setWordWrap(True)
    heading_text.addWidget(title)
    heading_text.addWidget(subtitle)
    heading.addLayout(heading_text, 1)
    card_layout.addLayout(heading)

    reason_text = str(reason or "").strip()
    if reason_text:
        reason_panel = QFrame()
        reason_panel.setObjectName("statusPanel")
        reason_layout = QVBoxLayout(reason_panel)
        reason_layout.setContentsMargins(14, 10, 14, 10)
        reason_layout.setSpacing(4)
        reason_title = QLabel("需要验证的原因")
        reason_title.setObjectName("statusEyebrow")
        reason_label = QLabel(reason_text)
        reason_label.setObjectName("statusText")
        reason_label.setWordWrap(True)
        reason_layout.addWidget(reason_title)
        reason_layout.addWidget(reason_label)
        card_layout.addWidget(reason_panel)

    instructions = QVBoxLayout()
    instructions.setSpacing(10)
    for number, text in (
        ("1", "点击下方按钮后，系统浏览器才会打开 Cloudflare 登录页。"),
        ("2", "完成企业邮箱验证码，再返回程序重新执行刚才的操作。"),
    ):
        row = QHBoxLayout()
        row.setSpacing(10)
        number_label = QLabel(number)
        number_label.setObjectName("stepNumber")
        number_label.setFixedSize(20, 20)
        number_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text_label = QLabel(text)
        text_label.setObjectName("stepText")
        text_label.setWordWrap(True)
        row.addWidget(number_label)
        row.addWidget(text_label, 1)
        instructions.addLayout(row)
    card_layout.addLayout(instructions)

    privacy = QLabel("程序不会在未经确认时自行打开网页。")
    privacy.setObjectName("hintText")
    card_layout.addWidget(privacy)

    buttons = QHBoxLayout()
    buttons.setSpacing(10)
    buttons.addStretch(1)
    cancel = QPushButton("稍后再说")
    cancel.setObjectName("secondaryButton")
    confirm = QPushButton("打开网页登录")
    confirm.setObjectName("primaryButton")
    confirm.setDefault(True)
    cancel.clicked.connect(dialog.reject)
    confirm.clicked.connect(dialog.accept)
    buttons.addWidget(cancel)
    buttons.addWidget(confirm)
    card_layout.addLayout(buttons)

    outer.addWidget(card)
    return dialog.exec() == QDialog.DialogCode.Accepted
