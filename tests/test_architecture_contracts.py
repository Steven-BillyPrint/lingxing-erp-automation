from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _resolved_imports(path: Path) -> set[str]:
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    package = ".".join(path.relative_to(ROOT).parent.parts)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if node.level:
            module = importlib.util.resolve_name(
                "." * node.level + module,
                package,
            )
        imports.add(module)
    return imports


def test_legacy_ui_contract_exports_are_identical() -> None:
    contract_controller = importlib.import_module(
        "erp_automation.contracts.controller"
    )
    contract_models = importlib.import_module("erp_automation.contracts.models")
    ui_controller = importlib.import_module("erp_automation.ui.controller")
    ui_models = importlib.import_module("erp_automation.ui.models")

    for name in ui_models.__all__:
        assert getattr(ui_models, name) is getattr(contract_models, name), name
    assert (
        ui_controller.BackgroundTaskController
        is contract_controller.BackgroundTaskController
    )
    assert ui_controller.ControlResult is contract_controller.ControlResult


def test_contract_package_import_does_not_load_ui_or_qt() -> None:
    script = """
import sys
import erp_automation.contracts

forbidden = sorted(
    name
    for name in sys.modules
    if name == "erp_automation.ui"
    or name.startswith("erp_automation.ui.")
    or name == "PySide6"
    or name.startswith("PySide6.")
)
if forbidden:
    raise SystemExit("unexpected imports: " + ", ".join(forbidden))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_application_and_coordination_do_not_import_ui() -> None:
    violations: list[str] = []
    for relative_root in (
        Path("erp_automation/application"),
        Path("erp_automation/coordination"),
    ):
        for path in sorted((ROOT / relative_root).rglob("*.py")):
            for imported in _resolved_imports(path):
                if imported == "erp_automation.ui" or imported.startswith(
                    "erp_automation.ui."
                ):
                    violations.append(
                        f"{path.relative_to(ROOT)} imports {imported}"
                    )
    assert not violations, "\n".join(violations)


def test_contract_layer_depends_only_on_stdlib_and_itself() -> None:
    violations: list[str] = []
    contract_root = ROOT / "erp_automation" / "contracts"
    for path in sorted(contract_root.rglob("*.py")):
        for imported in _resolved_imports(path):
            top_level = imported.partition(".")[0]
            if imported.startswith("erp_automation.contracts"):
                continue
            if top_level in sys.stdlib_module_names or top_level == "__future__":
                continue
            violations.append(f"{path.relative_to(ROOT)} imports {imported}")
    assert not violations, "\n".join(violations)
