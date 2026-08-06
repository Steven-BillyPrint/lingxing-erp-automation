from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from erp_automation import app
from erp_automation.runtime_mode import (
    expected_local_test_home,
    is_local_test_mode,
)


ROOT = Path(__file__).resolve().parents[1]
LOCAL_TEST_SCRIPT = ROOT / "scripts" / "start_local_test.ps1"


def test_local_test_mode_uses_only_the_fixed_isolated_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_appdata = tmp_path / "local-appdata"
    expected = local_appdata / "LingxingERP-LocalTest"
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("ERP_AUTOMATION_LOCAL_TEST", "1")
    monkeypatch.setenv("ERP_AUTOMATION_HOME", str(expected))
    monkeypatch.setattr(app.sys, "frozen", False, raising=False)

    assert is_local_test_mode()
    assert expected_local_test_home() == expected.resolve()
    assert app.resolve_workspace() == expected.resolve()


def test_local_test_mode_rejects_an_arbitrary_or_packaged_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-appdata"))
    monkeypatch.setenv("ERP_AUTOMATION_LOCAL_TEST", "1")
    monkeypatch.setenv("ERP_AUTOMATION_HOME", str(tmp_path / "wrong"))
    monkeypatch.setattr(app.sys, "frozen", False, raising=False)
    with pytest.raises(RuntimeError, match="必须固定"):
        app.resolve_workspace()

    monkeypatch.setenv(
        "ERP_AUTOMATION_HOME",
        str(tmp_path / "local-appdata" / "LingxingERP-LocalTest"),
    )
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    with pytest.raises(RuntimeError, match="只允许从源码启动"):
        app.resolve_workspace()


def test_local_test_mode_cannot_connect_to_the_deployed_shared_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ERP_AUTOMATION_LOCAL_TEST", "1")
    monkeypatch.setenv("ERP_AUTOMATION_SERVER_URL", "http://127.0.0.1:18765")

    with pytest.raises(RuntimeError, match="不能连接正式共享服务"):
        app.create_runtime_controller()


def test_candidate_release_and_profile_entry_points_are_removed() -> None:
    assert not (ROOT / "scripts" / "publish_client_candidate.ps1").exists()
    assert not (ROOT / "scripts" / "set_client_update_channel.ps1").exists()
    assert not (ROOT / "scripts" / "start_client_profile.ps1").exists()

    release_script = (ROOT / "scripts" / "publish_client_release.ps1").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "scripts" / "install_shared_client.ps1").read_text(
        encoding="utf-8"
    )
    updater = (ROOT / "scripts" / "update_shared_client.ps1").read_text(
        encoding="utf-8"
    )
    assert "ConfirmCandidateRelease" not in release_script
    assert "ConfirmProductionRelease" in release_script
    assert "ClientProfile" not in installer
    assert "UpdateChannel" not in updater
    assert "CandidateReleasesApiUrl" not in updater


def test_source_shortcuts_use_the_local_test_launcher() -> None:
    local_entry = (ROOT / "start_local_test.cmd").read_text(encoding="utf-8")
    legacy_entry = (ROOT / "start_shared_desktop.cmd").read_text(encoding="utf-8")
    package_script = (ROOT / "scripts" / "package_shared_client.ps1").read_text(
        encoding="utf-8"
    )

    assert "start_local_test.ps1" in local_entry
    assert "-ConfirmLocalTestRun" in local_entry
    assert "start_local_test.cmd" in legacy_entry
    assert "start_local_test.ps1" not in package_script


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher is required")
def test_local_test_launcher_requires_confirmation_and_reports_isolated_paths(
    tmp_path: Path,
) -> None:
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    environment = dict(os.environ)
    environment["LOCALAPPDATA"] = str(tmp_path / "local-appdata")
    base_command = [
        str(powershell),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(LOCAL_TEST_SCRIPT),
        "-PythonPath",
        sys.executable,
        "-ValidateOnly",
        "-OutputJson",
    ]
    rejected = subprocess.run(
        base_command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert rejected.returncode != 0

    accepted = subprocess.run(
        [*base_command, "-ConfirmLocalTestRun"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    payload = json.loads(accepted.stdout.strip().splitlines()[-1])
    assert payload["mode"] == "local_test"
    assert Path(payload["state_root"]) == (
        Path(environment["LOCALAPPDATA"]) / "LingxingERP-LocalTest"
    )
    assert Path(payload["formal_state_root"]) == (
        Path(environment["LOCALAPPDATA"]) / "LingxingERP"
    )
    assert payload["packaged_client"] is False
    assert payload["production_update_channel"] is False

    smoke = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LOCAL_TEST_SCRIPT),
            "-ConfirmLocalTestRun",
            "-PythonPath",
            sys.executable,
            "-ApplicationArguments",
            "--release-smoke-test",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert smoke.returncode == 0, smoke.stderr
    assert (Path(payload["state_root"]) / "data").is_dir()
    assert not Path(payload["formal_state_root"]).exists()
