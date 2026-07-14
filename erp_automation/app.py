from __future__ import annotations

import sys
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

from .application import DesktopApiServices, DesktopTaskRunner, ManagedApiErpMarkFunc
from .operations import cleanup_configured_log_roots
from .ui.controller import BackgroundTaskController
from .ui.persistent_controller import PersistentBackgroundTaskController
from .ui.qt_compat import PySide6RequiredError, require_pyside6


def resolve_workspace() -> Path:
    """Resolve the writable application home for source and packaged runs."""

    configured = str(os.environ.get("ERP_AUTOMATION_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def ensure_runtime_resources(workspace: Path) -> None:
    """Seed immutable packaged resources without replacing operator files."""

    if not getattr(sys, "frozen", False):
        return
    packaged_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
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
) -> PersistentBackgroundTaskController:
    """Return the encrypted, SQLite-backed controller with real task wiring."""

    application_home = Path(workspace).resolve() if workspace is not None else resolve_workspace()
    ensure_runtime_resources(application_home)
    controller = PersistentBackgroundTaskController(application_home)
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
        api_test=api_services.test_connection,
        custom_order_api_factory=api_services.custom_order_operations,
        erp_mark_func=erp_mark_func,
        runtime_write_guard_provider=lambda: not controller.snapshot().policy.emergency_stop_writes,
    )
    controller.attach_task_runner(task_runner)
    # Keep the service graph alive and available for API write adapters that
    # are injected after controller construction.
    controller.api_services = api_services
    controller.task_runner = task_runner
    return controller


def main(
    argv: Sequence[str] | None = None,
    *,
    controller: BackgroundTaskController | None = None,
) -> int:
    try:
        require_pyside6()
    except PySide6RequiredError as exc:
        print(f"桌面程序启动失败：{exc}", file=sys.stderr)
        return 2

    # Importing the Qt implementation is intentionally delayed so headless CLI
    # and test environments can import erp_automation.app without PySide6.
    from .ui.qt import run_desktop

    return run_desktop(
        controller or create_default_controller(),
        argv=list(argv) if argv is not None else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
