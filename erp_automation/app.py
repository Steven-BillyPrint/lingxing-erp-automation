from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

from .application import DesktopApiServices, DesktopTaskRunner, ManagedApiErpMarkFunc
from .configuration import EncryptedConfigurationStore
from .coordination.client_bootstrap import (
    bootstrap_packaged_shared_client,
    should_bootstrap_packaged_shared_client,
)
from .operations import cleanup_configured_log_roots
from .ui.controller import BackgroundTaskController
from .ui.models import TaskStatus
from .ui.persistent_controller import PersistentBackgroundTaskController
from .ui.qt_compat import PySide6RequiredError, require_pyside6


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
        "请重新安装最新版客户端，或联系管理员检查客户端凭据。"
    )
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    except Exception:
        print(message, file=sys.stderr)


class _PackagedStartupFeedback:
    def __init__(self, application, window, label, *, owns_application: bool) -> None:
        self.application = application
        self.window = window
        self.label = label
        self.owns_application = owns_application

    def update(self, message: str) -> None:
        self.label.setText(message)
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
    from PySide6.QtWidgets import (
        QApplication,
        QLabel,
        QProgressBar,
        QVBoxLayout,
        QWidget,
    )

    application = QApplication.instance()
    owns_application = application is None
    if application is None:
        application = QApplication([sys.executable, *argv])
        application.setFont(QFont("Microsoft YaHei UI", 9))
        application.setApplicationName("ERP 自动化控制台")
        application.setOrganizationName("ERP Automation")

    window = QWidget()
    window.setWindowTitle("ERP 自动化")
    window.setFixedSize(430, 122)
    layout = QVBoxLayout(window)
    layout.setContentsMargins(22, 18, 22, 18)
    layout.setSpacing(12)
    title = QLabel("ERP 自动化")
    title.setFont(QFont("Microsoft YaHei UI", 12, QFont.Weight.Bold))
    label = QLabel("正在准备启动…")
    progress = QProgressBar()
    progress.setRange(0, 0)
    progress.setFixedHeight(8)
    layout.addWidget(title)
    layout.addWidget(label)
    layout.addWidget(progress)
    window.show()
    application.processEvents()
    return _PackagedStartupFeedback(
        application,
        window,
        label,
        owns_application=owns_application,
    )


def resolve_workspace() -> Path:
    """Resolve the writable application home for source and packaged runs."""

    configured = str(os.environ.get("ERP_AUTOMATION_HOME") or "").strip()
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
) -> PersistentBackgroundTaskController:
    """Return the encrypted, SQLite-backed controller with real task wiring."""

    application_home = Path(workspace).resolve() if workspace is not None else resolve_workspace()
    ensure_runtime_resources(application_home)
    controller = PersistentBackgroundTaskController(
        application_home,
        config_store=config_store,
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
        shipment_notification_send=api_services.send_shipment_notifications,
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

    bootstrap_session = None
    startup_feedback = None
    execute_existing_application = False
    if controller is None and should_bootstrap_packaged_shared_client():
        try:
            startup_feedback = create_packaged_startup_feedback(effective_argv)
            execute_existing_application = startup_feedback.owns_application
            outcome = bootstrap_packaged_shared_client(
                instance_name=shared_instance_name,
                status_callback=startup_feedback.update,
            )
            if outcome.should_exit:
                startup_feedback.close()
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
            show_packaged_client_error(exc)
            return 5
        startup_feedback.close()

    # Importing the Qt implementation is intentionally delayed so headless CLI
    # and test environments can import erp_automation.app without PySide6.
    from .ui.qt import run_desktop

    try:
        return run_desktop(
            controller or create_runtime_controller(),
            argv=effective_argv,
            execute_existing_application=execute_existing_application,
        )
    finally:
        if bootstrap_session is not None:
            bootstrap_session.close()


if __name__ == "__main__":
    raise SystemExit(main())
