from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from erp_automation.client_version import CLIENT_VERSION as EMBEDDED_CLIENT_VERSION


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def _zip_content_sha256(package: Path) -> str:
    canonical = bytearray()
    with zipfile.ZipFile(package) as archive:
        names = sorted(
            (
                info.filename.replace("\\", "/")
                for info in archive.infolist()
                if not info.is_dir()
            ),
            key=lambda value: value.encode("utf-8"),
        )
        assert len(names) == len(set(names))
        for name in names:
            canonical.extend(name.encode("utf-8"))
            canonical.append(0)
            canonical.extend(hashlib.sha256(archive.read(name)).hexdigest().encode())
            canonical.extend(b"\n")
    return hashlib.sha256(canonical).hexdigest()


def _run_script(
    script: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        pytest.skip("PowerShell is required for Windows client release tests.")
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *map(str, arguments),
        ],
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        pytest.fail(
            "PowerShell release script failed.\n"
            f"command: {result.args!r}\n"
            f"exit code: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _build_dummy_release(tmp_path: Path) -> tuple[Path, Path, str]:
    version = (ROOT / "CLIENT_VERSION").read_text(encoding="utf-8").strip()
    built = tmp_path / "built" / "ERP自动化"
    built.mkdir(parents=True)
    # WScript.Shell validates shortcut targets on some clean Windows hosts.
    # Use a real PE executable so the installer test exercises the production
    # direct-EXE shortcut instead of depending on host-specific COM tolerance.
    shutil.copy2(sys.executable, built / "ERP自动化.exe")
    output = tmp_path / "release"
    output.mkdir()
    _run_script(
        ROOT / "scripts" / "package_shared_client.ps1",
        "-BuiltApplicationDir",
        str(built),
        "-Version",
        version,
        "-OutputDirectory",
        str(output),
        "-ArchiveName",
        "ERP-Automation-Client.zip",
    )
    package = output / "ERP-Automation-Client.zip"
    manifest = output / "latest.json"
    _run_script(
        ROOT / "scripts" / "create_release_manifest.ps1",
        "-PackagePath",
        str(package),
        "-Version",
        version,
        "-OutputPath",
        str(manifest),
    )
    return package, manifest, version


def _read_shortcut(path: Path, *, env: dict[str, str]) -> tuple[str, str]:
    inspection_env = dict(env)
    inspection_env["ERP_TEST_SHORTCUT"] = str(path)
    script = (
        "$path = $env:ERP_TEST_SHORTCUT;"
        "$shell = New-Object -ComObject Shell.Application;"
        "$folder = $shell.NameSpace([IO.Path]::GetDirectoryName($path));"
        "$item = $folder.ParseName([IO.Path]::GetFileName($path));"
        "$shortcut = $item.GetLink;"
        "@($shortcut.Path, $shortcut.Arguments) | ForEach-Object {"
        "'value:' + [Convert]::ToBase64String("
        "[Text.Encoding]::UTF8.GetBytes([string]$_))"
        "}"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", script],
        cwd=ROOT,
        env=inspection_env,
        text=True,
        encoding="ascii",
        capture_output=True,
        check=True,
    )
    values = [
        base64.b64decode(line.removeprefix("value:")).decode("utf-8")
        for line in result.stdout.splitlines()
        if line.startswith("value:")
    ]
    assert len(values) == 2
    return values[0], values[1]


def test_declared_client_version_uses_release_number_format() -> None:
    version = (ROOT / "CLIENT_VERSION").read_text(encoding="utf-8").strip()
    parts = version.split(".")

    assert len(parts) == 4
    assert all(part.isdigit() for part in parts)
    assert len(parts[0]) == 4
    assert len(parts[1]) == 2
    assert len(parts[2]) == 2
    assert EMBEDDED_CLIENT_VERSION == version


def test_launcher_and_updater_use_modern_custom_dialogs() -> None:
    launcher = (
        ROOT / "scripts" / "start_shared_desktop.ps1"
    ).read_text(encoding="utf-8")
    updater = (
        ROOT / "scripts" / "update_shared_client.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "System.Drawing.Size(520, 252)" in launcher
    assert "Drawing.Color]::FromArgb(244, 247, 252)" in launcher
    assert "$card.BorderStyle = 'FixedSingle'" in launcher
    assert "$statusPanel.BorderStyle = 'FixedSingle'" in launcher
    assert "$progressFill.BackColor = [Drawing.Color]::FromArgb(47, 111, 237)" in launcher

    confirmation = updater.split(
        "function Show-UpdateConfirmation",
        1,
    )[1].split("function New-DownloadWindow", 1)[0]
    assert "[System.Windows.Forms.MessageBox]::Show" not in confirmation
    assert "'发现新版本'" in confirmation
    assert "'立即更新'" in confirmation
    assert "'退出程序'" in confirmation
    assert "System.Drawing.Size(540, 336)" in confirmation

    download = updater.split(
        "function New-DownloadWindow",
        1,
    )[1].split("function Set-DownloadProgress", 1)[0]
    assert "'正在更新 ERP 自动化'" in download
    assert "'下载与完整性校验完成后，程序会自动重新打开。'" in download
    assert "System.Drawing.Size(520, 250)" in download
    assert "ProgressFill = $progressFill" in download


@pytest.mark.skipif(sys.platform != "win32", reason="Windows packaging is required")
def test_package_and_manifest_use_stable_release_asset_names(tmp_path: Path) -> None:
    package, manifest_path, version = _build_dummy_release(tmp_path)

    with zipfile.ZipFile(package) as archive:
        names = {name.replace("\\", "/") for name in archive.namelist()}
    assert "VERSION.txt" in names
    assert "dist/ERP自动化/ERP自动化.exe" in names
    assert "scripts/install_shared_client.ps1" in names
    assert "scripts/start_shared_desktop.ps1" in names
    assert "scripts/update_shared_client.ps1" in names
    assert "scripts/promote_portable_client.ps1" in names

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    assert manifest == {
        "schema_version": 1,
        "version": version,
        "published_at": manifest["published_at"],
        "mandatory": True,
        "package": {
            "name": "ERP-Automation-Client.zip",
            "url": (
                "https://github.com/Steven-BillyPrint/"
                f"lingxing-erp-automation/releases/download/v{version}/"
                "ERP-Automation-Client.zip"
            ),
            "sha256": digest,
            "content_sha256": _zip_content_sha256(package),
            "size": package.stat().st_size,
        },
    }
    datetime.fromisoformat(manifest["published_at"].replace("Z", "+00:00"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer is required")
def test_public_package_installs_without_embedding_or_creating_credentials(
    tmp_path: Path,
) -> None:
    package, _manifest_path, version = _build_dummy_release(tmp_path)
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(package) as archive:
        names = {name.replace("\\", "/").casefold() for name in archive.namelist()}
        archive.extractall(extracted)
    sensitive_names = {
        "coordination-token",
        "server-tunnel-ed25519",
        "known_hosts",
        "config.enc",
        ".env",
    }
    assert not any(
        Path(name).name.casefold() in sensitive_names for name in names
    )

    local_appdata = tmp_path / "fresh-local-appdata"
    desktop = tmp_path / "desktop"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)
    _run_script(
        extracted / "scripts" / "install_shared_client.ps1",
        "-PackageRoot",
        str(extracted),
        "-DesktopDirectory",
        str(desktop),
        "-Silent",
        env=env,
    )

    installed_root = local_appdata / "Programs" / "LingxingERP" / version
    assert (
        installed_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    ).is_file()
    state_root = local_appdata / "LingxingERP"
    assert state_root.is_dir()
    for sensitive_name in sensitive_names:
        assert not (state_root / sensitive_name).exists()
    shortcut = desktop / "ERP自动化（阿里云共享）.lnk"
    assert shortcut.is_file()
    shortcut_target, shortcut_arguments = _read_shortcut(shortcut, env=env)
    assert Path(shortcut_target) == (
        installed_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    )
    assert shortcut_arguments == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer is required")
def test_new_installer_promotes_a_legacy_non_git_portable_client(
    tmp_path: Path,
) -> None:
    package, _manifest_path, version = _build_dummy_release(tmp_path)
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(package) as archive:
        archive.extractall(extracted)
    old_root = tmp_path / "old-portable"
    old_application = old_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    old_application.parent.mkdir(parents=True)
    old_application.write_bytes(b"old-executable")
    old_scripts = old_root / "scripts"
    old_scripts.mkdir()
    for script_name in (
        "start_shared_desktop.ps1",
        "install_shared_client.ps1",
        "update_shared_client.ps1",
        "promote_portable_client.ps1",
    ):
        (old_scripts / script_name).write_text(
            f"old {script_name}\n",
            encoding="utf-8",
        )
    local_appdata = tmp_path / "local-appdata"
    desktop = tmp_path / "desktop"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)

    _run_script(
        extracted / "scripts" / "install_shared_client.ps1",
        "-PackageRoot",
        str(extracted),
        "-DesktopDirectory",
        str(desktop),
        "-Silent",
        env=env,
        cwd=old_root,
    )

    installed_application = (
        local_appdata
        / "Programs"
        / "LingxingERP"
        / version
        / "dist"
        / "ERP自动化"
        / "ERP自动化.exe"
    )
    expected_hash = hashlib.sha256(installed_application.read_bytes()).hexdigest()
    expected_updater = extracted.joinpath(
        "scripts",
        "update_shared_client.ps1",
    ).read_bytes()

    def promotion_completed() -> bool:
        try:
            return (
                hashlib.sha256(old_application.read_bytes()).hexdigest()
                == expected_hash
                and old_scripts.joinpath("update_shared_client.ps1").read_bytes()
                == expected_updater
            )
        except (FileNotFoundError, PermissionError):
            return False

    deadline = time.monotonic() + 10
    while not promotion_completed() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert promotion_completed()
    assert not (old_root / "VERSION.txt").exists()
    assert (
        old_scripts.joinpath("update_shared_client.ps1")
        .read_bytes()
        == expected_updater
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_updater_installs_atomically_and_uses_24_hour_cache(tmp_path: Path) -> None:
    package, manifest_path, version = _build_dummy_release(tmp_path)
    old_root = tmp_path / "old-client"
    old_root.mkdir()
    old_version = "2026.07.24.2"
    old_version_file = old_root / "VERSION.txt"
    old_version_file.write_text(old_version + "\n", encoding="utf-8")
    old_application = old_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    old_application.parent.mkdir(parents=True)
    old_application.write_bytes(b"old-executable")
    old_scripts = old_root / "scripts"
    old_scripts.mkdir()
    for script_name in (
        "start_shared_desktop.ps1",
        "install_shared_client.ps1",
        "update_shared_client.ps1",
        "promote_portable_client.ps1",
    ):
        shutil.copy2(ROOT / "scripts" / script_name, old_scripts / script_name)

    local_appdata = tmp_path / "local-appdata"
    state_root = local_appdata / "LingxingERP"
    state_root.mkdir(parents=True)
    (state_root / "server-tunnel-ed25519").write_text("private", encoding="utf-8")
    (state_root / "server-tunnel-ed25519.pub").write_text("public", encoding="utf-8")
    (state_root / "known_hosts").write_text("host", encoding="utf-8")
    (state_root / "coordination-token").write_text("t" * 48, encoding="utf-8")
    desktop = tmp_path / "desktop"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)

    installed = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersionFile",
        str(old_version_file),
        "-ManifestFile",
        str(manifest_path),
        "-PackageFile",
        str(package),
        "-StateRoot",
        str(state_root),
        "-DesktopDirectory",
        str(desktop),
        "-AssumeYes",
        "-OutputJson",
        env=env,
    )
    result = json.loads(installed.stdout)
    assert result["status"] == "updated"
    assert result["current_version"] == old_version
    assert result["latest_version"] == version
    installed_root = local_appdata / "Programs" / "LingxingERP" / version
    assert (installed_root / "VERSION.txt").read_text(encoding="utf-8").strip() == version
    assert (installed_root / "dist" / "ERP自动化" / "ERP自动化.exe").is_file()
    receipt_path = installed_root / "install-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == 1
    assert receipt["version"] == version
    assert receipt["package_sha256"] == hashlib.sha256(
        package.read_bytes()
    ).hexdigest()
    assert receipt["content_sha256"] == _zip_content_sha256(package)
    assert receipt["file_count"] > 5
    shortcut_path = desktop / "ERP自动化（阿里云共享）.lnk"
    assert shortcut_path.is_file()
    shortcut_target, shortcut_arguments = _read_shortcut(shortcut_path, env=env)
    assert Path(shortcut_target) == (
        installed_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    )
    assert shortcut_arguments == ""

    installed_exe = installed_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    installed_hash = hashlib.sha256(installed_exe.read_bytes()).hexdigest()
    package.unlink()
    blocker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        reused = _run_script(
            ROOT / "scripts" / "update_shared_client.ps1",
            "-CurrentVersion",
            old_version,
            "-CurrentPackageRoot",
            str(old_root),
            "-CurrentProcessId",
            str(blocker.pid),
            "-ManifestFile",
            str(manifest_path),
            "-PackageFile",
            str(package),
            "-StateRoot",
            str(state_root),
            "-DesktopDirectory",
            str(desktop),
            "-AssumeYes",
            "-OutputJson",
            env=env,
        )
    finally:
        blocker.terminate()
        blocker.wait(timeout=10)
    reused_result = json.loads(reused.stdout)
    assert reused_result["status"] == "updated"
    assert Path(reused_result["application_path"]) == installed_exe
    assert hashlib.sha256(installed_exe.read_bytes()).hexdigest() == installed_hash
    promotion_deadline = time.monotonic() + 10
    while (
        (
            hashlib.sha256(old_application.read_bytes()).hexdigest() != installed_hash
            or old_version_file.read_text(encoding="utf-8").strip() != version
        )
        and time.monotonic() < promotion_deadline
    ):
        time.sleep(0.05)
    assert hashlib.sha256(old_application.read_bytes()).hexdigest() == installed_hash
    assert old_version_file.read_text(encoding="utf-8").strip() == version

    known_outdated = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        old_version,
        "-ManifestUrl",
        "http://127.0.0.1:9/unavailable",
        "-StateRoot",
        str(state_root),
        "-OutputJson",
        env=env,
        check=False,
    )
    assert known_outdated.returncode != 0
    assert "必需更新" in (known_outdated.stderr + known_outdated.stdout)

    cached = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersionFile",
        str(installed_root / "VERSION.txt"),
        "-ManifestUrl",
        "http://127.0.0.1:9/unavailable",
        "-StateRoot",
        str(state_root),
        "-OutputJson",
        env=env,
    )
    assert json.loads(cached.stdout)["status"] == "current_cached"

    state_path = state_root / "update-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["last_successful_check_utc"] = (
        datetime.now(timezone.utc) - timedelta(hours=25)
    ).isoformat()
    state_path.write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )
    expired = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersionFile",
        str(installed_root / "VERSION.txt"),
        "-ManifestUrl",
        "http://127.0.0.1:9/unavailable",
        "-StateRoot",
        str(state_root),
        "-OutputJson",
        env=env,
        check=False,
    )
    assert expired.returncode != 0
    assert "24" in (expired.stderr + expired.stdout)

    installed_exe.write_bytes(b"tampered")
    rejected_reuse = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        old_version,
        "-ManifestFile",
        str(manifest_path),
        "-PackageFile",
        str(package),
        "-StateRoot",
        str(state_root),
        "-AssumeYes",
        "-OutputJson",
        env=env,
        check=False,
    )
    assert rejected_reuse.returncode != 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
@pytest.mark.parametrize("git_marker", ["none", "directory", "file"])
@pytest.mark.parametrize("version_file_present", [False, True])
def test_portable_client_promotion_updates_same_entry_point_after_exit(
    tmp_path: Path,
    git_marker: str,
    version_file_present: bool,
) -> None:
    source = tmp_path / "installed" / "2026.07.30.6"
    target = tmp_path / "portable"
    source_application = source / "dist" / "ERP自动化" / "ERP自动化.exe"
    target_application = target / "dist" / "ERP自动化" / "ERP自动化.exe"
    source_application.parent.mkdir(parents=True)
    target_application.parent.mkdir(parents=True)
    source_application.write_bytes(b"new-executable")
    target_application.write_bytes(b"old-executable")
    (source / "VERSION.txt").write_text("2026.07.30.6\n", encoding="utf-8")
    target_version = target / "VERSION.txt"
    if version_file_present:
        target_version.write_text("stale-version\n", encoding="utf-8")
    source_scripts = source / "scripts"
    target_scripts = target / "scripts"
    source_scripts.mkdir()
    target_scripts.mkdir()
    for script_name in (
        "start_shared_desktop.ps1",
        "install_shared_client.ps1",
        "update_shared_client.ps1",
        "promote_portable_client.ps1",
    ):
        (source_scripts / script_name).write_text(
            f"new {script_name}\n",
            encoding="utf-8",
        )
        (target_scripts / script_name).write_text(
            f"old {script_name}\n",
            encoding="utf-8",
        )
    if git_marker == "directory":
        (target / ".git").mkdir()
    elif git_marker == "file":
        (target / ".git").write_text(
            "gitdir: ../worktrees/portable\n",
            encoding="utf-8",
        )
    local_appdata = tmp_path / "local-appdata"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)

    _run_script(
        ROOT / "scripts" / "promote_portable_client.ps1",
        "-SourcePackageRoot",
        str(source),
        "-TargetPackageRoot",
        str(target),
        "-ExpectedCurrentVersion",
        "2026.07.30.5",
        "-ExpectedVersion",
        "2026.07.30.6",
        "-ExpectedTargetSha256",
        hashlib.sha256(b"old-executable").hexdigest(),
        "-WaitProcessId",
        "0",
        env=env,
    )

    assert target_application.read_bytes() == b"new-executable"
    if version_file_present:
        assert target_version.read_text(encoding="utf-8").strip() == (
            "2026.07.30.6"
        )
    else:
        assert not target_version.exists()
    expected_script_prefix = "old" if git_marker != "none" else "new"
    assert (
        target_scripts.joinpath("update_shared_client.ps1")
        .read_text(encoding="utf-8")
        .startswith(expected_script_prefix)
    )
    assert not list(target.glob(".erp-client-promote-*"))
    assert not list(target.glob(".erp-client-backup-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_updater_rejects_an_exe_newer_than_the_published_release(
    tmp_path: Path,
) -> None:
    _package, manifest_path, _version = _build_dummy_release(tmp_path)
    state_root = tmp_path / "local-appdata" / "LingxingERP"
    result = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        "2099.01.01.1",
        "-ManifestFile",
        str(manifest_path),
        "-StateRoot",
        str(state_root),
        "-OutputJson",
        check=False,
    )

    assert result.returncode != 0
    assert "高于正式发布版本" in (result.stderr + result.stdout)
