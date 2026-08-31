from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from .application import DesktopApiServices, DesktopTaskRunner, ManagedApiErpMarkFunc
from .configuration import (
    EncryptedConfigurationStore,
    MigrationScope,
    MigrationValidationError,
    PortableMigrationService,
)
from .coordination.client_bootstrap import (
    SERVER_HOST,
    SERVER_USER,
    ClientAccessSetupResult,
    ClientUpdateResult,
    PackagedClientPaths,
    bootstrap_local_test_shared_client,
    bootstrap_packaged_shared_client,
    resolve_packaged_client_paths,
    run_client_update,
    should_bootstrap_packaged_shared_client,
    start_updated_client,
)
from .operations import cleanup_configured_log_roots
from .runtime_mode import (
    expected_local_test_home,
    is_local_test_mode,
    is_local_test_shared_server_mode,
)
from .ui.controller import BackgroundTaskController
from .ui.models import TaskStatus
from .ui.persistent_controller import PersistentBackgroundTaskController
from .ui.qt_compat import PySide6RequiredError, require_pyside6


PACKAGED_STARTUP_DIALOG_DELAY_SECONDS = 0.75


def consume_shared_instance_name(argv: Sequence[str]) -> tuple[list[str], str]:
    """Remove the shortcut-only instance name option before Qt sees argv."""

    cleaned: list[str] = []
    instance_name = ""
    index = 0
    while index < len(argv):
        argument = str(argv[index])
        if argument == "--shared-instance-name":
            if index + 1 >= len(argv):
                raise ValueError("--shared-instance-name requires a value.")
            instance_name = str(argv[index + 1]).strip()
            index += 2
            continue
        cleaned.append(argument)
        index += 1
    return cleaned, instance_name


def show_packaged_client_error(error: BaseException) -> None:
    """Show a useful error even though the packaged application has no console."""

    title = "ERP 自动化"
    message = (
        "无法启动阿里云共享客户端。\n\n"
        f"{error}\n\n"
        "请按上方具体提示处理后重试；若仍无法启动，请复制诊断信息交给管理员。"
    )
    try:
        from PySide6.QtWidgets import QApplication

        application = QApplication.instance()
        owns_application = application is None
        if application is None:
            application = QApplication([])
        from .ui.modern_dialogs import show_packaged_client_error_dialog

        show_packaged_client_error_dialog(message)
        if owns_application:
            application.quit()
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
        except Exception:
            print(message, file=sys.stderr)


class _PackagedStartupFeedback:
    def __init__(
        self,
        application,
        window,
        label,
        *,
        owns_application: bool,
        show_delay_seconds: float = PACKAGED_STARTUP_DIALOG_DELAY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.application = application
        self.window = window
        self.label = label
        self.owns_application = owns_application
        self._show_delay_seconds = max(0.0, float(show_delay_seconds))
        self._clock = clock
        self._created_at = float(clock())
        self._shown = False

    def update(self, message: str) -> None:
        self.label.setText(message)
        if (
            self.window is not None
            and not self._shown
            and float(self._clock()) - self._created_at
            >= self._show_delay_seconds
        ):
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
            self._shown = True
        self.application.processEvents()

    def close(self) -> None:
        if self.window is not None:
            self.window.close()
            self.window.deleteLater()
            self.application.processEvents()
            self.window = None


def create_packaged_startup_feedback(
    argv: Sequence[str],
) -> _PackagedStartupFeedback:
    """Display immediate progress while the EXE updates and creates tunnels."""

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from .ui.modern_dialogs import build_packaged_startup_dialog

    application = QApplication.instance()
    owns_application = application is None
    if application is None:
        application = QApplication([sys.executable, *argv])
        application.setFont(QFont("Microsoft YaHei UI", 9))
        application.setApplicationName("ERP 自动化控制台")
        application.setOrganizationName("ERP Automation")

    window, label = build_packaged_startup_dialog()
    return _PackagedStartupFeedback(
        application,
        window,
        label,
        owns_application=owns_application,
    )


def prompt_cloudflare_access_login(
    reason: str = "",
    *,
    parent=None,
) -> bool:
    """Ask before opening the company-email login page."""

    from .ui.modern_dialogs import confirm_cloudflare_access_login

    return confirm_cloudflare_access_login(reason, parent=parent)


def prompt_packaged_client_access(
    paths: PackagedClientPaths,
) -> ClientAccessSetupResult:
    """Require explicit authorization before a public download can connect."""

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog,
        QFileDialog,
        QFormLayout,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QVBoxLayout,
    )

    from .coordination.access_profile import (
        install_client_access_files,
        install_client_access_profile,
        load_client_access_profile,
    )

    dialog = QDialog()
    setup_result = ClientAccessSetupResult()
    dialog.setWindowTitle("首次使用授权")
    dialog.resize(720, 420)
    layout = QVBoxLayout(dialog)
    title = QLabel("这份公开客户端不包含任何公司账号或服务器凭据")
    title.setStyleSheet("font-size: 16px; font-weight: bold;")
    layout.addWidget(title)
    explanation = QLabel(
        "必须先导入管理员提供的加密客户端授权文件，或手工选择 SSH 私钥、"
        "固定主机指纹并填写协调服务 Token，程序才会连接阿里云共享后台。"
        "如果授权文件包含设置备份，将在企业邮箱登录成功后自动恢复到该账号。"
        "授权文件可在不同电脑导入，因此持有人等同获得公司系统访问权，请单独保管。"
    )
    explanation.setWordWrap(True)
    layout.addWidget(explanation)

    import_button = QPushButton("导入加密客户端授权文件…")
    import_button.setObjectName("primaryButton")
    layout.addWidget(import_button)
    divider = QLabel("或手工填写授权材料")
    divider.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(divider)

    form = QFormLayout()
    key_edit = QLineEdit()
    known_hosts_edit = QLineEdit()
    token_edit = QLineEdit()
    token_edit.setEchoMode(QLineEdit.EchoMode.Password)

    def path_row(editor: QLineEdit, title_text: str) -> QHBoxLayout:
        row = QHBoxLayout()
        browse = QPushButton("选择…")

        def choose() -> None:
            selected, _filter = QFileDialog.getOpenFileName(
                dialog,
                title_text,
                "",
                "所有文件 (*)",
            )
            if selected:
                editor.setText(selected)

        browse.clicked.connect(choose)
        row.addWidget(editor, 1)
        row.addWidget(browse)
        return row

    form.addRow("SSH 私钥", path_row(key_edit, "选择 SSH 私钥"))
    form.addRow(
        "服务器固定主机指纹",
        path_row(known_hosts_edit, "选择 known_hosts"),
    )
    form.addRow("协调服务 Token", token_edit)
    layout.addLayout(form)

    button_row = QHBoxLayout()
    manual_button = QPushButton("保存手工授权")
    cancel_button = QPushButton("取消")
    button_row.addStretch(1)
    button_row.addWidget(manual_button)
    button_row.addWidget(cancel_button)
    layout.addLayout(button_row)

    def import_profile() -> None:
        nonlocal setup_result
        source, _filter = QFileDialog.getOpenFileName(
            dialog,
            "选择加密客户端授权文件",
            "",
            "ERP 客户端授权文件 (*.erp-client);;所有文件 (*)",
        )
        if not source:
            return
        passphrase, accepted = QInputDialog.getText(
            dialog,
            "客户端授权文件密码",
            "输入授权文件密码（至少 12 个字符）：",
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        try:
            profile = load_client_access_profile(source, passphrase)
            if profile.configuration_package:
                with tempfile.TemporaryDirectory(
                    prefix="erp-client-profile-check-"
                ) as directory:
                    package_path = Path(directory) / "settings.erp-migrate"
                    package_path.write_bytes(profile.configuration_package)
                    validated = PortableMigrationService().validate_package(
                        package_path,
                        passphrase,
                    )
                    if validated.manifest.scope is not MigrationScope.CONFIGURATION_ONLY:
                        raise MigrationValidationError(
                            "客户端授权文件内只允许包含服务器设置，不允许携带业务状态。"
                        )
            install_client_access_profile(
                profile,
                state_root=paths.state_root,
                expected_server_host=SERVER_HOST,
                expected_server_user=SERVER_USER,
            )
            setup_result = ClientAccessSetupResult(
                accepted=True,
                configuration_package=bytes(profile.configuration_package),
                passphrase=(passphrase if profile.configuration_package else ""),
            )
        except Exception as exc:
            QMessageBox.critical(dialog, "授权导入失败", str(exc))
            return
        dialog.accept()

    def install_manual() -> None:
        nonlocal setup_result
        try:
            private_key = Path(key_edit.text().strip()).read_bytes()
            known_hosts = Path(known_hosts_edit.text().strip()).read_bytes()
            install_client_access_files(
                state_root=paths.state_root,
                ssh_private_key=private_key,
                known_hosts=known_hosts,
                coordination_token=token_edit.text(),
            )
            setup_result = ClientAccessSetupResult(accepted=True)
        except Exception as exc:
            QMessageBox.critical(dialog, "手工授权失败", str(exc))
            return
        dialog.accept()

    import_button.clicked.connect(import_profile)
    manual_button.clicked.connect(install_manual)
    cancel_button.clicked.connect(dialog.reject)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return setup_result
    return ClientAccessSetupResult()


def resolve_workspace() -> Path:
    """Resolve the writable application home for source and packaged runs."""

    configured = str(os.environ.get("ERP_AUTOMATION_HOME") or "").strip()
    if is_local_test_mode():
        if getattr(sys, "frozen", False):
            raise RuntimeError("本机测试运行只允许从源码启动，不能由正式 EXE 开启。")
        expected = expected_local_test_home()
        if configured and Path(configured).expanduser().resolve() != expected:
            raise RuntimeError(
                "本机测试配置目录必须固定为 "
                f"{expected}，不能指向正式版或任意目录。"
            )
        return expected
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        if executable_dir.parent.name.casefold() == "dist":
            return executable_dir.parent.parent
        return executable_dir
    return Path(__file__).resolve().parents[1]


def ensure_runtime_resources(workspace: Path) -> None:
    """Seed immutable packaged resources without replacing operator files."""

    packaged_root = (
        Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[1]
    )
    if workspace == packaged_root:
        return
    for relative in (
        Path("data/china_workdays.json"),
        Path("rules/sku_rules.example.json"),
        Path("rules/split_rules.example.json"),
    ):
        source = packaged_root / relative
        destination = workspace / relative
        if source.is_file() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def create_default_controller(
    workspace: str | Path | None = None,
    *,
    config_store: EncryptedConfigurationStore | None = None,
    delegate_browser_actions: bool = False,
    recover_interrupted_task_journal: bool = True,
) -> PersistentBackgroundTaskController:
    """Return the encrypted, SQLite-backed controller with real task wiring."""

    application_home = Path(workspace).resolve() if workspace is not None else resolve_workspace()
    ensure_runtime_resources(application_home)
    controller = PersistentBackgroundTaskController(
        application_home,
        config_store=config_store,
        recover_interrupted_task_journal=recover_interrupted_task_journal,
    )
    # Log retention is deliberately confined to the application's own fixed
    # directories.  A malformed/legacy config must never turn Documents or a
    # drive root into a recursive deletion target during startup.
    log_dir = application_home / "logs"
    cleanup_configured_log_roots(
        SimpleNamespace(
            log_dir=log_dir,
            debug_log_dir=application_home / "debug" / "logs",
        ),
        retention_days=90,
    )
    api_services = DesktopApiServices(
        application_home,
        configuration_store=controller.config_store,
        policy_provider=lambda: controller.snapshot().policy,
    )
    async def create_erp_gateway():
        return await api_services.create_gateway(controller.snapshot().settings)

    erp_mark_func = ManagedApiErpMarkFunc(
        create_erp_gateway,
        controller.configuration_values,
    )
    task_runner = DesktopTaskRunner(
        application_home,
        settings_provider=lambda: controller.snapshot().settings,
        configuration_provider=controller.configuration_values,
        custom_scan=api_services.scan_custom_orders,
        shipment_scan=api_services.scan_shipments,
        shipment_notification_sync=api_services.sync_shipment_notifications,
        shipment_notification_review_send=(
            lambda notification_id, retry, actor: controller._send_shipment_notification(
                notification_id,
                retry=retry,
                actor=actor,
                # A batch task owns only the provider-acceptance step.  Delivery
                # receipts are refreshed independently, so cancellation can
                # take effect between messages instead of waiting for polling.
                wait_for_delivery=False,
            )
        ),
        shipment_notification_contact_refresh=(
            api_services.refresh_shipment_notification_contacts
        ),
        api_test=api_services.test_connection,
        custom_order_api_factory=api_services.custom_order_operations,
        custom_order_status_check=api_services.get_custom_order_processing_status,
        erp_mark_func=erp_mark_func,
        runtime_write_guard_provider=lambda: not controller.snapshot().policy.emergency_stop_writes,
        interaction_handler=controller.request_interaction,
        cancellation_provider=controller.cancellation_requested,
        progress_handler=lambda task_id, message, percent: controller.set_task_status(
            task_id,
            TaskStatus.RUNNING,
            message=message,
            progress_percent=percent,
        ),
        order_detail_lookup=api_services.get_order_detail_payload,
        customer_shipping_list_probe=api_services.probe_customer_shipping_list,
        delegate_browser_actions=delegate_browser_actions,
    )
    controller.attach_task_runner(task_runner)
    # Keep the service graph alive and available for API write adapters that
    # are injected after controller construction.
    controller.api_services = api_services
    controller.task_runner = task_runner
    return controller


def create_runtime_controller(
    workspace: str | Path | None = None,
) -> BackgroundTaskController:
    """Select the shared server controller when remote mode is configured."""

    server_url = str(os.environ.get("ERP_AUTOMATION_SERVER_URL") or "").strip()
    local_test_mode = is_local_test_mode()
    local_test_shared_server = is_local_test_shared_server_mode()
    if local_test_mode and server_url:
        if not local_test_shared_server:
            raise RuntimeError(
                "本机测试只能通过受控启动器连接正式共享服务。"
            )
        parsed_server_url = urlsplit(server_url)
        if (
            parsed_server_url.scheme != "http"
            or parsed_server_url.hostname != "127.0.0.1"
            or parsed_server_url.port is None
            or parsed_server_url.username is not None
            or parsed_server_url.password is not None
            or parsed_server_url.query
            or parsed_server_url.fragment
            or parsed_server_url.path not in {"", "/"}
        ):
            raise RuntimeError(
                "本机测试的正式共享服务连接必须使用受控的本机 SSH 隧道。"
            )
    elif local_test_shared_server:
        raise RuntimeError("本机测试的正式共享服务隧道尚未建立。")
    if not server_url:
        return create_default_controller(workspace)
    from .coordination import RemoteBackgroundTaskController

    token = str(os.environ.get("ERP_AUTOMATION_SERVER_TOKEN") or "").strip()
    token_file = str(os.environ.get("ERP_AUTOMATION_SERVER_TOKEN_FILE") or "").strip()
    if not token and token_file:
        token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    ca_file = str(os.environ.get("ERP_AUTOMATION_SERVER_CA_FILE") or "").strip()
    return RemoteBackgroundTaskController(
        server_url,
        token=token,
        ca_file=ca_file or None,
        display_name=str(
            os.environ.get("ERP_AUTOMATION_INSTANCE_NAME")
            or os.environ.get("USERNAME")
            or "ERP desktop"
        ).strip(),
        instance_id=str(os.environ.get("ERP_AUTOMATION_INSTANCE_ID") or "").strip() or None,
        browser_endpoint=str(
            os.environ.get("ERP_AUTOMATION_BROWSER_ENDPOINT") or ""
        ).strip(),
        browser_local_port=int(
            str(os.environ.get("ERP_AUTOMATION_BROWSER_LOCAL_PORT") or "0")
        ),
        logistics_browser_endpoint=str(
            os.environ.get("ERP_AUTOMATION_LOGISTICS_BROWSER_ENDPOINT") or ""
        ).strip(),
        logistics_browser_local_port=int(
            str(
                os.environ.get("ERP_AUTOMATION_LOGISTICS_BROWSER_LOCAL_PORT")
                or "0"
            )
        ),
        client_version=str(
            os.environ.get("ERP_AUTOMATION_CLIENT_VERSION") or ""
        ).strip(),
        browser_profile_dir=Path(
            os.environ.get("ERP_AUTOMATION_BROWSER_PROFILE")
            or Path(os.environ.get("LOCALAPPDATA") or resolve_workspace())
            / "LingxingERP"
            / "browser-profile"
        ),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    controller: BackgroundTaskController | None = None,
) -> int:
    effective_argv = list(argv) if argv is not None else list(sys.argv[1:])
    try:
        effective_argv, shared_instance_name = consume_shared_instance_name(
            effective_argv
        )
    except ValueError as exc:
        show_packaged_client_error(exc)
        return 5
    if "--release-smoke-test" in effective_argv:
        smoke_controller = controller or create_default_controller()
        try:
            snapshot = smoke_controller.snapshot()
            if snapshot is None:
                return 3
            prepare_close = getattr(smoke_controller, "prepare_close", None)
            if callable(prepare_close) and not prepare_close().accepted:
                return 4
            return 0
        finally:
            close_controller = getattr(smoke_controller, "close", None)
            if callable(close_controller):
                close_controller()
    try:
        require_pyside6()
    except PySide6RequiredError as exc:
        print(f"桌面程序启动失败：{exc}", file=sys.stderr)
        return 2

    single_instance_guard = None
    production_client = bool(
        controller is None
        and getattr(sys, "frozen", False)
        and not is_local_test_mode()
    )
    if production_client:
        from .crash_diagnostics import install_crash_diagnostics
        from .single_instance import (
            acquire_desktop_single_instance,
            activate_existing_desktop_window,
        )

        try:
            single_instance_guard = acquire_desktop_single_instance()
        except OSError as exc:
            show_packaged_client_error(
                RuntimeError(f"无法建立客户端单实例保护：{exc}")
            )
            return 5
        if not single_instance_guard.acquired:
            activate_existing_desktop_window()
            return 0
        install_crash_diagnostics()

    def release_single_instance() -> None:
        nonlocal single_instance_guard
        if single_instance_guard is not None:
            single_instance_guard.close()
            single_instance_guard = None

    bootstrap_session = None
    startup_feedback = None
    execute_existing_application = False
    packaged_bootstrap_requested = bool(
        controller is None and should_bootstrap_packaged_shared_client()
    )
    local_test_bootstrap_requested = bool(
        controller is None and is_local_test_shared_server_mode()
    )
    if packaged_bootstrap_requested or local_test_bootstrap_requested:
        try:
            startup_feedback = create_packaged_startup_feedback(effective_argv)
            execute_existing_application = startup_feedback.owns_application
            if local_test_bootstrap_requested:
                local_test_instance_name = (
                    str(os.environ.get("USERNAME") or "").strip()
                    or "ERP desktop"
                ) + "（本机测试）"
                outcome = bootstrap_local_test_shared_client(
                    instance_name=local_test_instance_name,
                    status_callback=startup_feedback.update,
                    access_login_callback=prompt_cloudflare_access_login,
                )
            else:
                outcome = bootstrap_packaged_shared_client(
                    instance_name=shared_instance_name,
                    status_callback=startup_feedback.update,
                    access_setup_callback=prompt_packaged_client_access,
                    access_login_callback=prompt_cloudflare_access_login,
                )
            if outcome.should_exit:
                startup_feedback.close()
                release_single_instance()
                return 0
            if outcome.session is None:
                raise RuntimeError("共享客户端启动结果缺少有效会话。")
            bootstrap_session = outcome.session
            controller = bootstrap_session.controller
        except Exception as exc:
            if startup_feedback is not None:
                startup_feedback.close()
            if bootstrap_session is not None:
                bootstrap_session.close()
            release_single_instance()
            show_packaged_client_error(exc)
            return 5
        startup_feedback.close()

    # Importing the Qt implementation is intentionally delayed so headless CLI
    # and test environments can import erp_automation.app without PySide6.
    from .ui.qt import run_desktop

    runtime_restart_application: Path | None = None

    def install_required_client_update(
        required_version: str,
    ) -> ClientUpdateResult:
        paths = resolve_packaged_client_paths(require_access_files=False)
        result = run_client_update(paths)
        if (
            str(required_version or "").strip()
            and result.latest_version != str(required_version).strip()
        ):
            raise RuntimeError(
                "正式更新通道版本与服务器要求不一致："
                f"{result.latest_version or '未知'} / {required_version}"
            )
        return result

    def schedule_runtime_restart(application_path: Path) -> None:
        nonlocal runtime_restart_application
        runtime_restart_application = application_path

    try:
        exit_code = run_desktop(
            controller or create_runtime_controller(),
            argv=effective_argv,
            execute_existing_application=execute_existing_application,
            required_client_update_handler=(
                install_required_client_update
                if packaged_bootstrap_requested and bootstrap_session is not None
                else None
            ),
            runtime_restart_callback=(
                schedule_runtime_restart
                if packaged_bootstrap_requested and bootstrap_session is not None
                else None
            ),
        )
    finally:
        if bootstrap_session is not None:
            bootstrap_session.close()
        release_single_instance()
    if runtime_restart_application is not None:
        start_updated_client(runtime_restart_application)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
