from __future__ import annotations

from pathlib import Path

from erp_automation import app


def test_workspace_environment_override_has_highest_priority(monkeypatch, tmp_path):
    configured = tmp_path / "configured-home"
    monkeypatch.setenv("ERP_AUTOMATION_HOME", str(configured))
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app.sys, "executable", str(tmp_path / "dist" / "app.exe"))

    assert app.resolve_workspace() == configured.resolve()


def test_frozen_dist_layout_uses_project_root(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    executable = project_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    monkeypatch.delenv("ERP_AUTOMATION_HOME", raising=False)
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app.sys, "executable", str(executable))

    assert app.resolve_workspace() == project_root.resolve()


def test_frozen_standalone_layout_uses_executable_directory(monkeypatch, tmp_path):
    executable = tmp_path / "standalone" / "ERP自动化.exe"
    monkeypatch.delenv("ERP_AUTOMATION_HOME", raising=False)
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app.sys, "executable", str(executable))

    assert app.resolve_workspace() == executable.parent.resolve()


def test_source_layout_uses_repository_root(monkeypatch):
    monkeypatch.delenv("ERP_AUTOMATION_HOME", raising=False)
    monkeypatch.setattr(app.sys, "frozen", False, raising=False)

    assert app.resolve_workspace() == Path(app.__file__).resolve().parents[1]
