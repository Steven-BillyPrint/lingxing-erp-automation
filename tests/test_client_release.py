from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
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
        entries = sorted(
            (
                (info.filename.replace("\\", "/"), info)
                for info in archive.infolist()
                if not info.is_dir()
            ),
            key=lambda value: value[0].encode("utf-8"),
        )
        names = [name for name, _info in entries]
        assert len(names) == len(set(names))
        for name, info in entries:
            canonical.extend(name.encode("utf-8"))
            canonical.append(0)
            canonical.extend(hashlib.sha256(archive.read(info)).hexdigest().encode())
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


def _build_dummy_release(
    tmp_path: Path,
    *,
    smoke_exit_code: int = 0,
    smoke_delay_ms: int = 0,
) -> tuple[Path, Path, str]:
    version = (ROOT / "CLIENT_VERSION").read_text(encoding="utf-8").strip()
    built = tmp_path / "built" / "ERP自动化"
    built.mkdir(parents=True)
    # Build a tiny real PE so installer tests exercise both Windows shortcut
    # creation and the same --release-smoke-test gate used in production.
    compiler_env = dict(os.environ)
    compiler_env["ERP_TEST_OUTPUT_EXE"] = str(built / "ERP自动化.exe")
    compiler_env["ERP_TEST_CSHARP"] = (
        "public static class Program {"
        " public static int Main(string[] args) {"
        " if (args.Length > 0 && args[0] == \"--hold-open\") {"
        "  System.Threading.Thread.Sleep(30000);"
        " }"
        f" System.Threading.Thread.Sleep({int(smoke_delay_ms)});"
        f" return {int(smoke_exit_code)};"
        " }"
        "}"
    )
    compiled = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-Command",
            (
                "Add-Type -TypeDefinition $env:ERP_TEST_CSHARP "
                "-Language CSharp "
                "-OutputAssembly $env:ERP_TEST_OUTPUT_EXE "
                "-OutputType ConsoleApplication"
            ),
        ],
        cwd=ROOT,
        env=compiler_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if compiled.returncode:
        pytest.fail(
            "Failed to compile the installer smoke-test executable.\n"
            f"stdout:\n{compiled.stdout}\nstderr:\n{compiled.stderr}"
        )
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


def _write_manifest_for_package(
    path: Path,
    package: Path,
    version: str,
    *,
    content_sha256: str | None = None,
) -> Path:
    payload = {
        "schema_version": 1,
        "version": version,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "mandatory": True,
        "package": {
            "name": "ERP-Automation-Client.zip",
            "url": (
                "https://github.com/Steven-BillyPrint/"
                f"lingxing-erp-automation/releases/download/v{version}/"
                "ERP-Automation-Client.zip"
            ),
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "content_sha256": content_sha256 or _zip_content_sha256(package),
            "size": package.stat().st_size,
        },
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


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


def test_production_publisher_reuses_exact_successful_ci_run() -> None:
    publisher = (
        ROOT / "scripts" / "publish_client_release.ps1"
    ).read_text(encoding="utf-8-sig")
    workflow = (
        ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    ci_workflow = (
        ROOT / ".github" / "workflows" / "test.yml"
    ).read_text(encoding="utf-8")

    assert "& gh workflow run release.yml" in publisher
    assert '--field "release_commit=$localCommit"' in publisher
    assert '--field "ci_run_id=$ciRunId"' in publisher
    assert '--field "request_id=$releaseRequestId"' in publisher
    assert "function Get-ReusableCiRun" in publisher
    assert "--workflow test.yml" in publisher
    assert "--commit $Commit" in publisher
    assert "[string]$run.headSha -ne $Commit" in publisher
    assert "[string]$run.conclusion -ne 'success'" in publisher
    assert "--json databaseId,displayTitle,status,url,createdAt" in publisher
    assert "[string]$_.displayTitle -eq $expectedRunTitle" in publisher
    assert "$runsBefore = @($parsedRunsBefore)" in publisher
    assert "$runs = @($parsedRuns)" in publisher
    assert (
        '@(($runsBeforeOutput -join "`n") | ConvertFrom-Json)'
        not in publisher
    )
    assert "gh run watch ([string]$releaseRun.databaseId)" in publisher
    assert "/actions/runs/(?<id>" not in publisher
    assert "ref: ${{ inputs.release_commit }}" in workflow
    assert "ci_run_id:" in workflow
    assert (
        "run-name: Build client release "
        "${{ inputs.release_commit }} [${{ inputs.request_id }}]"
        in workflow
    )
    assert "$checkedOutCommit -ne $env:RELEASE_COMMIT" in workflow
    assert "git merge-base --is-ancestor $checkedOutCommit origin/main" in workflow
    assert "/actions/runs/$env:CI_RUN_ID" in workflow
    assert "/actions/workflows/test.yml" in workflow
    assert "[int64]$run.workflow_id -ne [int64]$workflow.id" in workflow
    assert "[string]$run.head_repository.full_name" in workflow
    assert "[string]$run.head_branch -ne 'main'" in workflow
    assert "[string]$run.head_sha -ne $env:RELEASE_COMMIT" in workflow
    assert "[string]$run.conclusion -ne 'success'" in workflow
    assert "gh run download $env:CI_RUN_ID" in workflow
    assert 'ERP-Automation-Client-$env:RELEASE_COMMIT' in workflow
    assert "ref: main" not in workflow
    assert "python -m pytest" not in workflow
    assert "python -m PyInstaller" not in workflow
    assert "Reusable CI manifest does not match the packaged client" in workflow
    assert "Reusable CI SHA256SUMS.txt does not match" in workflow

    tests_job = ci_workflow.split("  windows-tests:", 1)[1].split(
        "  windows-client-build:", 1
    )[0]
    build_job = ci_workflow.split("  windows-client-build:", 1)[1]
    assert "Run complete test suite" in tests_job
    assert "python -m pytest -q" in tests_job
    assert "needs:" not in build_job
    assert "python -m PyInstaller" in build_job
    assert "Run packaged smoke test" in build_job
    assert "Run full updater and installer smoke test" in build_job
    assert "-ManifestFile" in build_job
    assert "-PackageFile" in build_job
    assert "SHA256SUMS.txt does not match the release manifest" in build_job
    assert "name: ERP-Automation-Client-${{ github.sha }}" in build_job
    assert "overwrite: true" in build_job


def test_candidate_release_is_opt_in_and_promotes_the_same_assets() -> None:
    publisher = (
        ROOT / "scripts" / "publish_client_release.ps1"
    ).read_text(encoding="utf-8-sig")
    candidate_entry = (
        ROOT / "scripts" / "publish_client_candidate.ps1"
    ).read_text(encoding="utf-8-sig")
    updater = (
        ROOT / "scripts" / "update_shared_client.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "[switch]$ConfirmCandidateRelease" in publisher
    assert "--prerelease=true" in publisher
    assert "--prerelease=false" in publisher
    assert "--latest=false" in publisher
    assert "候选版本必须高于当前正式更新通道" in publisher
    assert "-ConfirmCandidateRelease" in candidate_entry
    assert "releases/latest/download/latest.json" in updater
    assert "releases?per_page=20" in updater
    assert "if ($_.draft" in updater
    assert "Compare-ClientVersion $Matches[1] $StableVersion" in updater
    assert "$effectiveUpdateChannel -ne 'candidate'" in updater
    assert "allow_candidate_rollback" in updater


def test_production_publisher_uses_supported_release_list_fields() -> None:
    publisher = (
        ROOT / "scripts" / "publish_client_release.ps1"
    ).read_text(encoding="utf-8-sig")

    release_list = publisher.split(
        "$releasesOutput = & gh release list",
        1,
    )[1].split("if ($LASTEXITCODE -ne 0)", 1)[0]
    assert "--json tagName,isDraft,isPrerelease,publishedAt" in release_list
    assert ",url" not in release_list


def test_production_rollout_activates_latest_only_after_server_health() -> None:
    publisher = (
        ROOT / "scripts" / "publish_client_release.ps1"
    ).read_text(encoding="utf-8-sig")
    deployer = (
        ROOT / "scripts" / "deploy_production.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "--draft=false" in publisher
    assert "--prerelease=false" in publisher
    assert "--latest=false" in publisher
    assert "gh release edit $tag --draft=false --latest\n" not in publisher

    deployment_index = deployer.rindex(
        "$deployment = Invoke-ControlledDeploymentSsh"
    )
    activation_index = deployer.index("& gh release edit $tag --latest")
    verification_index = deployer.index(
        "& gh release view --json tagName,url",
        activation_index,
    )
    rollout_index = deployer.rindex(
        "[void](Complete-ServerRollout $localCommit $version)"
    )
    assert (
        deployment_index
        < activation_index
        < verification_index
        < rollout_index
    )
    assert "[string]$latestRelease.tagName -ne $tag" in deployer
    assert "[string]$release.targetCommitish -ne $localCommit" in deployer
    assert "git merge-base --is-ancestor" not in deployer

    server_deployer = (
        ROOT / "deploy" / "server" / "deploy_current.sh"
    ).read_text(encoding="utf-8")
    assert 'previous_client_version="$(' in server_deployer
    assert "/etc/lingxing-erp/previous-client-version" in server_deployer
    assert 'rollout_deadline_epoch="pending"' in server_deployer
    assert (
        "ERP_ROLLOUT_PREVIOUS_CLIENT_VERSION_FILE="
        in (
            ROOT / "deploy" / "server" / "coordination.env.example"
        ).read_text(encoding="utf-8")
    )


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
    assert "scripts/set_client_update_channel.ps1" in names
    assert "scripts/promote_portable_client.ps1" in names
    assert "scripts/complete_client_repair.ps1" in names

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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_candidate_channel_requires_explicit_local_enrollment(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    setter = ROOT / "scripts" / "set_client_update_channel.ps1"

    rejected = _run_script(
        setter,
        "-Channel",
        "Candidate",
        "-StateRoot",
        str(state_root),
        "-OutputJson",
        check=False,
    )
    assert rejected.returncode != 0
    assert not (state_root / "update-channel.json").exists()

    enrolled = _run_script(
        setter,
        "-Channel",
        "Candidate",
        "-StateRoot",
        str(state_root),
        "-ConfirmCandidateEnrollment",
        "-OutputJson",
    )
    enrolled_result = json.loads(enrolled.stdout)
    assert enrolled_result["previous_channel"] == "stable"
    assert enrolled_result["channel"] == "candidate"
    configuration = json.loads(
        (state_root / "update-channel.json").read_text(encoding="utf-8-sig")
    )
    assert configuration["channel"] == "candidate"
    assert configuration["allow_candidate_rollback"] is False

    rollback_rejected = _run_script(
        setter,
        "-Channel",
        "Stable",
        "-StateRoot",
        str(state_root),
        "-OutputJson",
        check=False,
    )
    assert rollback_rejected.returncode != 0

    rollback = _run_script(
        setter,
        "-Channel",
        "Stable",
        "-StateRoot",
        str(state_root),
        "-ConfirmCandidateRollback",
        "-OutputJson",
    )
    rollback_result = json.loads(rollback.stdout)
    assert rollback_result["channel"] == "stable"
    assert rollback_result["candidate_rollback_authorized"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_candidate_channel_is_reported_by_updater(tmp_path: Path) -> None:
    _package, manifest_path, version = _build_dummy_release(tmp_path)
    state_root = tmp_path / "state"
    _run_script(
        ROOT / "scripts" / "set_client_update_channel.ps1",
        "-Channel",
        "Candidate",
        "-StateRoot",
        str(state_root),
        "-ConfirmCandidateEnrollment",
    )

    checked = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        "2000.01.01.1",
        "-ManifestFile",
        str(manifest_path),
        "-StateRoot",
        str(state_root),
        "-CheckOnly",
        "-OutputJson",
    )
    result = json.loads(checked.stdout)
    assert result == {
        "status": "update_required",
        "current_version": "2000.01.01.1",
        "latest_version": version,
        "update_channel": "candidate",
        "release_channel": "candidate",
        "launcher_path": "",
        "application_path": "",
    }


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
@pytest.mark.parametrize("prerelease", [True, False])
def test_candidate_channel_selects_latest_unactivated_release_without_moving_stable(
    tmp_path: Path,
    prerelease: bool,
) -> None:
    stable_version = "2000.01.01.1"
    candidate_version = "2099.01.01.2"

    def manifest(version: str) -> dict[str, object]:
        package_url = (
            "https://github.com/Steven-BillyPrint/lingxing-erp-automation/"
            f"releases/download/v{version}/ERP-Automation-Client.zip"
        )
        return {
            "schema_version": 1,
            "version": version,
            "mandatory": True,
            "package": {
                "name": "ERP-Automation-Client.zip",
                "url": package_url,
                "sha256": "1" * 64,
                "content_sha256": "2" * 64,
                "size": 1024,
            },
        }

    requests: list[str] = []
    stable_manifest = manifest(stable_version)
    candidate_manifest = manifest(candidate_version)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            requests.append(self.path)
            base_url = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/stable.json":
                payload: object = stable_manifest
            elif self.path == "/candidate.json":
                payload = candidate_manifest
            elif self.path == "/releases":
                payload = [
                    {
                        "draft": False,
                        "prerelease": prerelease,
                        "published_at": "2099-01-01T00:00:00Z",
                        "tag_name": f"v{candidate_version}",
                        "assets": [
                            {
                                "name": "latest.json",
                                "browser_download_url": f"{base_url}/candidate.json",
                            },
                            {
                                "name": "ERP-Automation-Client.zip",
                                "browser_download_url": candidate_manifest["package"][
                                    "url"
                                ],
                            },
                        ],
                    }
                ]
            else:
                self.send_error(404)
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        stable = _run_script(
            ROOT / "scripts" / "update_shared_client.ps1",
            "-CurrentVersion",
            "1999.01.01.1",
            "-ManifestUrl",
            f"{base_url}/stable.json",
            "-CandidateReleasesApiUrl",
            f"{base_url}/releases",
            "-UpdateChannel",
            "Stable",
            "-StateRoot",
            str(tmp_path / "stable-state"),
            "-CheckOnly",
            "-OutputJson",
        )
        stable_result = json.loads(stable.stdout)
        assert stable_result["latest_version"] == stable_version
        assert stable_result["release_channel"] == "stable"
        assert requests == ["/stable.json"]

        requests.clear()
        candidate = _run_script(
            ROOT / "scripts" / "update_shared_client.ps1",
            "-CurrentVersion",
            "1999.01.01.1",
            "-ManifestUrl",
            f"{base_url}/stable.json",
            "-CandidateReleasesApiUrl",
            f"{base_url}/releases",
            "-UpdateChannel",
            "Candidate",
            "-StateRoot",
            str(tmp_path / "candidate-state"),
            "-CheckOnly",
            "-OutputJson",
        )
        candidate_result = json.loads(candidate.stdout)
        assert candidate_result["latest_version"] == candidate_version
        assert candidate_result["release_channel"] == "candidate"
        assert requests == ["/stable.json", "/releases", "/candidate.json"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_explicit_candidate_rollback_can_restore_lower_stable_version(
    tmp_path: Path,
) -> None:
    package, manifest_path, stable_version = _build_dummy_release(tmp_path)
    candidate_version = "2099.01.01.2"
    local_appdata = tmp_path / "local-appdata"
    state_root = local_appdata / "LingxingERP"
    state_root.mkdir(parents=True)
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)
    (state_root / "update-state.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "last_successful_check_utc": datetime.now(timezone.utc).isoformat(),
                "latest_version": candidate_version,
                "package_sha256": "1" * 64,
                "content_sha256": "2" * 64,
                "manifest_url": "candidate-test",
                "channel": "candidate",
            }
        ),
        encoding="utf-8",
    )
    _run_script(
        ROOT / "scripts" / "set_client_update_channel.ps1",
        "-Channel",
        "Candidate",
        "-StateRoot",
        str(state_root),
        "-ConfirmCandidateEnrollment",
    )
    _run_script(
        ROOT / "scripts" / "set_client_update_channel.ps1",
        "-Channel",
        "Stable",
        "-StateRoot",
        str(state_root),
        "-ConfirmCandidateRollback",
    )

    rolled_back = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        candidate_version,
        "-ManifestFile",
        str(manifest_path),
        "-PackageFile",
        str(package),
        "-StateRoot",
        str(state_root),
        "-DesktopDirectory",
        str(tmp_path / "desktop"),
        "-AssumeYes",
        "-SkipApplicationSmokeTest",
        "-OutputJson",
        env=env,
    )
    result = json.loads(rolled_back.stdout)
    assert result["status"] == "updated"
    assert result["latest_version"] == stable_version
    assert result["update_channel"] == "stable"
    configuration = json.loads(
        (state_root / "update-channel.json").read_text(encoding="utf-8-sig")
    )
    assert configuration["channel"] == "stable"
    assert configuration["allow_candidate_rollback"] is False
    update_state = json.loads(
        (state_root / "update-state.json").read_text(encoding="utf-8-sig")
    )
    assert update_state["latest_version"] == stable_version
    assert update_state["channel"] == "stable"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_first_candidate_can_reuse_pre_channel_stable_install_on_rollback(
    tmp_path: Path,
) -> None:
    package, _manifest_path, stable_version = _build_dummy_release(tmp_path)
    candidate_version = "2099.01.01.3"
    local_appdata = tmp_path / "local-appdata"
    state_root = local_appdata / "LingxingERP"
    installed_root = (
        local_appdata / "Programs" / "LingxingERP" / stable_version
    )
    with zipfile.ZipFile(package) as archive:
        archive.extractall(installed_root)
    (installed_root / "scripts" / "set_client_update_channel.ps1").unlink()
    legacy_installer = installed_root / "scripts" / "install_shared_client.ps1"
    legacy_installer_text = legacy_installer.read_text(encoding="utf-8-sig")
    for added_line in (
        "$sourceChannelSetter = Join-Path $sourceRoot "
        "'scripts\\set_client_update_channel.ps1'\n",
        "    $sourceChannelSetter,\n",
        "            'scripts\\set_client_update_channel.ps1',\n",
    ):
        assert added_line in legacy_installer_text
        legacy_installer_text = legacy_installer_text.replace(added_line, "")
    legacy_installer.write_text(legacy_installer_text, encoding="utf-8-sig")

    legacy_package = tmp_path / "legacy-stable.zip"
    installed_files = sorted(
        path for path in installed_root.rglob("*") if path.is_file()
    )
    with zipfile.ZipFile(
        legacy_package,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for path in installed_files:
            archive.write(path, path.relative_to(installed_root).as_posix())
    legacy_manifest = _write_manifest_for_package(
        tmp_path / "legacy-stable.json",
        legacy_package,
        stable_version,
    )
    manifest = json.loads(legacy_manifest.read_text(encoding="utf-8"))
    (installed_root / "install-receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": stable_version,
                "package_sha256": manifest["package"]["sha256"],
                "content_sha256": manifest["package"]["content_sha256"],
                "file_count": len(installed_files),
            }
        ),
        encoding="utf-8",
    )
    state_root.mkdir(parents=True)
    (state_root / "update-state.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "last_successful_check_utc": datetime.now(timezone.utc).isoformat(),
                "latest_version": candidate_version,
                "package_sha256": "3" * 64,
                "content_sha256": "4" * 64,
                "manifest_url": "candidate-test",
                "channel": "candidate",
            }
        ),
        encoding="utf-8",
    )
    _run_script(
        ROOT / "scripts" / "set_client_update_channel.ps1",
        "-Channel",
        "Candidate",
        "-StateRoot",
        str(state_root),
        "-ConfirmCandidateEnrollment",
    )
    _run_script(
        ROOT / "scripts" / "set_client_update_channel.ps1",
        "-Channel",
        "Stable",
        "-StateRoot",
        str(state_root),
        "-ConfirmCandidateRollback",
    )

    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)
    rolled_back = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        candidate_version,
        "-ManifestFile",
        str(legacy_manifest),
        "-StateRoot",
        str(state_root),
        "-DesktopDirectory",
        str(tmp_path / "desktop"),
        "-AssumeYes",
        "-OutputJson",
        env=env,
    )
    result = json.loads(rolled_back.stdout)
    assert result["status"] == "updated"
    assert result["latest_version"] == stable_version
    assert result["application_path"].lower().endswith("erp自动化.exe")
    assert not (
        installed_root / "scripts" / "set_client_update_channel.ps1"
    ).exists()
    configuration = json.loads(
        (state_root / "update-channel.json").read_text(encoding="utf-8-sig")
    )
    assert configuration["allow_candidate_rollback"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer is required")
def test_public_package_installs_without_embedding_or_creating_credentials(
    tmp_path: Path,
) -> None:
    package, manifest_path, version = _build_dummy_release(tmp_path)
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

    # A manually extracted package has no outer ZIP hash while installing.
    # Its first normal update check verifies the full installed tree, repairs
    # harmless VERSION metadata, and converges on the same receipt as an
    # automatically installed client without downloading the package again.
    (installed_root / "VERSION.txt").unlink()
    current = _run_script(
        installed_root / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        version,
        "-CurrentPackageRoot",
        str(installed_root),
        "-ManifestFile",
        str(manifest_path),
        "-StateRoot",
        str(state_root),
        "-OutputJson",
        env=env,
    )
    assert json.loads(current.stdout)["status"] == "current"
    assert (installed_root / "VERSION.txt").read_text(
        encoding="utf-8"
    ).strip() == version
    receipt = json.loads(
        (installed_root / "install-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["version"] == version
    assert receipt["package_sha256"] == hashlib.sha256(
        package.read_bytes()
    ).hexdigest()
    assert receipt["content_sha256"] == _zip_content_sha256(package)

    installed_exe = installed_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    installed_exe.write_bytes(b"tampered-after-install")
    repaired = _run_script(
        installed_root / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        version,
        "-CurrentPackageRoot",
        str(installed_root),
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
    assert json.loads(repaired.stdout)["status"] == "updated"
    assert hashlib.sha256(installed_exe.read_bytes()).hexdigest() == hashlib.sha256(
        (
            extracted / "dist" / "ERP自动化" / "ERP自动化.exe"
        ).read_bytes()
    ).hexdigest()
    assert json.loads(
        (installed_root / "install-receipt.json").read_text(encoding="utf-8")
    )["content_sha256"] == _zip_content_sha256(package)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer is required")
def test_initial_install_smoke_failure_creates_no_program_entry(
    tmp_path: Path,
) -> None:
    package, _manifest_path, version = _build_dummy_release(
        tmp_path,
        smoke_exit_code=6,
    )
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(package) as archive:
        archive.extractall(extracted)
    local_appdata = tmp_path / "local-appdata"
    desktop = tmp_path / "desktop"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)

    result = _run_script(
        extracted / "scripts" / "install_shared_client.ps1",
        "-PackageRoot",
        str(extracted),
        "-DesktopDirectory",
        str(desktop),
        "-Silent",
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "安装前启动自检失败" in (result.stdout + result.stderr)
    assert not (
        local_appdata / "Programs" / "LingxingERP" / version
    ).exists()
    assert not (desktop / "ERP自动化（阿里云共享）.lnk").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer is required")
def test_initial_install_smoke_timeout_terminates_without_activation(
    tmp_path: Path,
) -> None:
    package, _manifest_path, version = _build_dummy_release(
        tmp_path,
        smoke_delay_ms=5_000,
    )
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(package) as archive:
        archive.extractall(extracted)
    local_appdata = tmp_path / "local-appdata"
    desktop = tmp_path / "desktop"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)

    started = time.monotonic()
    result = _run_script(
        extracted / "scripts" / "install_shared_client.ps1",
        "-PackageRoot",
        str(extracted),
        "-DesktopDirectory",
        str(desktop),
        "-ApplicationSmokeTestTimeoutSeconds",
        "1",
        "-Silent",
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert time.monotonic() - started < 4
    assert "启动自检超时" in (result.stdout + result.stderr)
    assert not (
        local_appdata / "Programs" / "LingxingERP" / version
    ).exists()
    assert not (desktop / "ERP自动化（阿里云共享）.lnk").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer is required")
@pytest.mark.parametrize("git_worktree", [False, True])
def test_new_installer_promotes_a_legacy_portable_client(
    tmp_path: Path,
    git_worktree: bool,
) -> None:
    package, _manifest_path, version = _build_dummy_release(tmp_path)
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(package) as archive:
        archive.extractall(extracted)
    old_root = tmp_path / "old-portable"
    old_application = old_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    old_application.parent.mkdir(parents=True)
    old_application.write_bytes(b"old-executable")
    old_application_hash = hashlib.sha256(b"old-executable").hexdigest()
    old_scripts = old_root / "scripts"
    old_scripts.mkdir()
    for script_name in (
        "start_shared_desktop.ps1",
        "install_shared_client.ps1",
        "update_shared_client.ps1",
        "promote_portable_client.ps1",
        "complete_client_repair.ps1",
    ):
        (old_scripts / script_name).write_text(
            f"old {script_name}\n",
            encoding="utf-8",
        )
    old_updater = old_scripts.joinpath("update_shared_client.ps1").read_bytes()
    old_version_file = old_root / "VERSION.txt"
    if git_worktree:
        (old_root / ".git").mkdir()
        old_version_file.write_text("2026.07.30.2\n", encoding="utf-8")
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
    expected_channel_setter = extracted.joinpath(
        "scripts",
        "set_client_update_channel.ps1",
    ).read_bytes()
    expected_target_updater = old_updater if git_worktree else expected_updater
    expected_target_hash = old_application_hash if git_worktree else expected_hash

    def promotion_completed() -> bool:
        try:
            return (
                hashlib.sha256(old_application.read_bytes()).hexdigest()
                == expected_target_hash
                and (
                    old_scripts.joinpath("update_shared_client.ps1").read_bytes()
                    == expected_target_updater
                )
                and (
                    not git_worktree
                    or old_version_file.read_text(encoding="utf-8").strip()
                    == "2026.07.30.2"
                )
            )
        except (FileNotFoundError, PermissionError):
            return False

    deadline = time.monotonic() + 10
    while not promotion_completed() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert promotion_completed()
    assert old_version_file.exists() is git_worktree
    assert (
        old_scripts.joinpath("update_shared_client.ps1")
        .read_bytes()
        == expected_target_updater
    )
    if git_worktree:
        assert not old_scripts.joinpath("set_client_update_channel.ps1").exists()
    else:
        assert (
            old_scripts.joinpath("set_client_update_channel.ps1").read_bytes()
            == expected_channel_setter
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
        "complete_client_repair.ps1",
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
    program_base = local_appdata / "Programs" / "LingxingERP"
    for stale_version in (
        "2026.07.20.1",
        "2026.07.21.1",
        "2026.07.22.1",
    ):
        stale_root = program_base / stale_version
        stale_root.mkdir(parents=True)
        (stale_root / "stale.txt").write_text("stale", encoding="utf-8")

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
        "-SkipApplicationSmokeTest",
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
    assert not (program_base / "2026.07.20.1").exists()
    assert not (program_base / "2026.07.21.1").exists()
    assert (program_base / "2026.07.22.1").is_dir()

    installed_exe = installed_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    installed_hash = hashlib.sha256(installed_exe.read_bytes()).hexdigest()
    package_bytes = package.read_bytes()
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
            "-SkipApplicationSmokeTest",
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

    def legacy_entry_promoted() -> bool:
        try:
            return (
                hashlib.sha256(old_application.read_bytes()).hexdigest()
                == installed_hash
                and old_version_file.read_text(encoding="utf-8").strip()
                == version
            )
        except (FileNotFoundError, PermissionError):
            # Directory promotion swaps the old and new trees after the
            # process exits; readers can observe that millisecond boundary.
            return False

    promotion_deadline = time.monotonic() + 10
    while not legacy_entry_promoted() and time.monotonic() < promotion_deadline:
        time.sleep(0.05)
    assert legacy_entry_promoted()

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
    package.write_bytes(package_bytes)
    repaired_reuse = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        old_version,
        "-ManifestFile",
        str(manifest_path),
        "-PackageFile",
        str(package),
        "-StateRoot",
        str(state_root),
        "-DesktopDirectory",
        str(desktop),
        "-AssumeYes",
        "-SkipApplicationSmokeTest",
        "-OutputJson",
        env=env,
    )
    assert json.loads(repaired_reuse.stdout)["status"] == "updated"
    assert hashlib.sha256(installed_exe.read_bytes()).hexdigest() == installed_hash


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_failed_new_exe_smoke_test_keeps_previous_shortcut(tmp_path: Path) -> None:
    package, manifest_path, _version = _build_dummy_release(
        tmp_path,
        smoke_exit_code=7,
    )
    old_version = "2026.07.24.2"
    old_root = tmp_path / "old-client"
    old_root.mkdir()
    old_version_file = old_root / "VERSION.txt"
    old_version_file.write_text(old_version + "\n", encoding="utf-8")

    local_appdata = tmp_path / "local-appdata"
    state_root = local_appdata / "LingxingERP"
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)
    shortcut = desktop / "ERP自动化（阿里云共享）.lnk"
    previous_shortcut = b"previous-shortcut-must-survive"
    shortcut.write_bytes(previous_shortcut)

    failed = _run_script(
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
        check=False,
    )

    assert failed.returncode != 0
    assert "启动自检失败" in (failed.stdout + failed.stderr)
    assert shortcut.read_bytes() == previous_shortcut


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer is required")
def test_same_version_repair_replaces_directory_atomically(tmp_path: Path) -> None:
    package, _manifest_path, version = _build_dummy_release(tmp_path)
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(package) as archive:
        archive.extractall(extracted)
    local_appdata = tmp_path / "local-appdata"
    desktop = tmp_path / "desktop"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)
    arguments = (
        "-PackageRoot",
        str(extracted),
        "-DesktopDirectory",
        str(desktop),
        "-SkipShortcut",
        "-Silent",
    )
    installer = extracted / "scripts" / "install_shared_client.ps1"
    _run_script(installer, *arguments, env=env)

    program_base = local_appdata / "Programs" / "LingxingERP"
    installed_root = program_base / version
    installed_exe = installed_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    expected_hash = hashlib.sha256(
        (extracted / "dist" / "ERP自动化" / "ERP自动化.exe").read_bytes()
    ).hexdigest()
    installed_exe.write_bytes(b"damaged")
    (installed_root / "obsolete.file").write_text("stale", encoding="utf-8")
    interrupted_backup = program_base / f".{version}.replace-interrupted"
    installed_root.rename(interrupted_backup)
    interrupted_stage = program_base / f".{version}.install-interrupted"
    interrupted_stage.mkdir()
    (interrupted_stage / "partial.file").write_text("partial", encoding="utf-8")

    _run_script(installer, *arguments, env=env)

    assert hashlib.sha256(installed_exe.read_bytes()).hexdigest() == expected_hash
    assert not (installed_root / "obsolete.file").exists()
    assert not interrupted_backup.exists()
    assert not interrupted_stage.exists()
    assert not list(program_base.glob(f".{version}.install-*"))
    assert not list(program_base.glob(f".{version}.replace-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_same_version_auto_repair_keeps_running_old_process_alive(
    tmp_path: Path,
) -> None:
    package, manifest_path, version = _build_dummy_release(tmp_path)
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(package) as archive:
        archive.extractall(extracted)
    local_appdata = tmp_path / "local-appdata"
    state_root = local_appdata / "LingxingERP"
    desktop = tmp_path / "desktop"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)
    installer = extracted / "scripts" / "install_shared_client.ps1"
    _run_script(
        installer,
        "-PackageRoot",
        str(extracted),
        "-DesktopDirectory",
        str(desktop),
        "-Silent",
        env=env,
    )
    installed_root = local_appdata / "Programs" / "LingxingERP" / version
    installed_exe = installed_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    (installed_root / "unexpected.file").write_text("damage", encoding="utf-8")
    running = subprocess.Popen(
        [str(installed_exe), "--hold-open"],
        cwd=installed_root,
        env=env,
    )
    try:
        time.sleep(0.2)
        result = _run_script(
            installed_root / "scripts" / "update_shared_client.ps1",
            "-CurrentVersion",
            version,
            "-CurrentPackageRoot",
            str(installed_root),
            "-CurrentProcessId",
            str(running.pid),
            "-ManifestFile",
            str(manifest_path),
            "-PackageFile",
            str(package),
            "-StateRoot",
            str(state_root),
            "-DesktopDirectory",
            str(desktop),
            "-AssumeYes",
            "-SkipApplicationSmokeTest",
            "-OutputJson",
            env=env,
        )
        assert json.loads(result.stdout)["status"] == "repair_scheduled"
        assert running.poll() is None
        running.terminate()
        running.wait(timeout=5)
        deadline = time.monotonic() + 15
        while (
            (installed_root / "unexpected.file").exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert not (installed_root / "unexpected.file").exists()
        receipt_path = installed_root / "install-receipt.json"
        while not receipt_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        receipt = json.loads(
            receipt_path.read_text(encoding="utf-8")
        )
        assert receipt["content_sha256"] == _zip_content_sha256(package)
    finally:
        if running.poll() is None:
            running.terminate()
        try:
            running.wait(timeout=5)
        except subprocess.TimeoutExpired:
            running.kill()
            running.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_concurrent_updates_share_one_verified_install(tmp_path: Path) -> None:
    package, manifest_path, version = _build_dummy_release(tmp_path)
    old_version = "2026.07.24.2"
    old_version_file = tmp_path / "VERSION.txt"
    old_version_file.write_text(old_version + "\n", encoding="utf-8")
    local_appdata = tmp_path / "local-appdata"
    state_root = local_appdata / "LingxingERP"
    desktop = tmp_path / "desktop"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)
    command = [
        POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ROOT / "scripts" / "update_shared_client.ps1"),
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
        "-SkipApplicationSmokeTest",
        "-OutputJson",
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=60) for process in processes]

    assert [process.returncode for process in processes] == [0, 0]
    payloads = [
        json.loads(stdout.strip().splitlines()[-1])
        for stdout, _stderr in results
    ]
    assert {payload["status"] for payload in payloads} == {"updated"}
    installed_root = local_appdata / "Programs" / "LingxingERP" / version
    assert (installed_root / "install-receipt.json").is_file()
    assert not list((state_root / "updates").glob("*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_same_version_portable_entry_converges_on_verified_release(
    tmp_path: Path,
) -> None:
    package, manifest_path, version = _build_dummy_release(tmp_path)
    portable = tmp_path / "portable"
    portable_application = portable / "dist" / "ERP自动化" / "ERP自动化.exe"
    portable_application.parent.mkdir(parents=True)
    portable_application.write_bytes(b"stale-portable-executable")
    scripts = portable / "scripts"
    scripts.mkdir()
    for script_name in (
        "start_shared_desktop.ps1",
        "install_shared_client.ps1",
        "update_shared_client.ps1",
        "promote_portable_client.ps1",
        "complete_client_repair.ps1",
    ):
        (scripts / script_name).write_bytes(
            (ROOT / "scripts" / script_name).read_bytes()
        )
    (portable / "VERSION.txt").write_text(version + "\n", encoding="utf-8")
    local_appdata = tmp_path / "local-appdata"
    state_root = local_appdata / "LingxingERP"
    desktop = tmp_path / "desktop"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)
    exited_parent = subprocess.Popen(
        [POWERSHELL, "-NoProfile", "-Command", "exit 0"],
        env=env,
    )
    exited_parent.wait(timeout=5)

    result = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        version,
        "-CurrentPackageRoot",
        str(portable),
        "-CurrentProcessId",
        str(exited_parent.pid),
        "-ManifestFile",
        str(manifest_path),
        "-PackageFile",
        str(package),
        "-StateRoot",
        str(state_root),
        "-DesktopDirectory",
        str(desktop),
        "-AssumeYes",
        "-SkipApplicationSmokeTest",
        "-OutputJson",
        env=env,
    )

    payload = json.loads(result.stdout)
    assert payload["status"] == "updated"
    expected = (
        local_appdata
        / "Programs"
        / "LingxingERP"
        / version
        / "dist"
        / "ERP自动化"
        / "ERP自动化.exe"
    ).read_bytes()
    deadline = time.monotonic() + 10
    while portable_application.read_bytes() != expected and time.monotonic() < deadline:
        time.sleep(0.05)
    assert portable_application.read_bytes() == expected


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
        "complete_client_repair.ps1",
    ):
        (source_scripts / script_name).write_text(
            f"new {script_name}\n",
            encoding="utf-8",
        )
        (target_scripts / script_name).write_text(
            f"old {script_name}\n",
            encoding="utf-8",
        )
    (source_scripts / "set_client_update_channel.ps1").write_text(
        "new set_client_update_channel.ps1\n",
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

    is_source_worktree = git_marker != "none"
    assert target_application.read_bytes() == (
        b"old-executable" if is_source_worktree else b"new-executable"
    )
    if version_file_present:
        assert target_version.read_text(encoding="utf-8").strip() == (
            "stale-version" if is_source_worktree else "2026.07.30.6"
        )
    else:
        assert not target_version.exists()
    expected_script_prefix = "old" if is_source_worktree else "new"
    assert (
        target_scripts.joinpath("update_shared_client.ps1")
        .read_text(encoding="utf-8")
        .startswith(expected_script_prefix)
    )
    channel_setter = target_scripts / "set_client_update_channel.ps1"
    if is_source_worktree:
        assert not channel_setter.exists()
    else:
        assert channel_setter.read_text(encoding="utf-8").startswith("new")
    assert not list(target.glob(".erp-client-promote-*"))
    assert not list(target.glob(".erp-client-backup-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_portable_promotion_recovers_interrupted_swap_before_retry(
    tmp_path: Path,
) -> None:
    source = tmp_path / "installed" / "2026.07.30.6"
    target = tmp_path / "portable"
    source_application = source / "dist" / "ERP自动化" / "ERP自动化.exe"
    target_application = target / "dist" / "ERP自动化" / "ERP自动化.exe"
    source_application.parent.mkdir(parents=True)
    target_application.parent.mkdir(parents=True)
    source_application.write_bytes(b"new-executable")
    target_application.write_bytes(b"partial-new-executable")
    (source / "VERSION.txt").write_text("2026.07.30.6\n", encoding="utf-8")
    (target / "VERSION.txt").write_text("2026.07.30.6\n", encoding="utf-8")
    source_scripts = source / "scripts"
    target_scripts = target / "scripts"
    source_scripts.mkdir()
    target_scripts.mkdir()
    script_names = (
        "start_shared_desktop.ps1",
        "install_shared_client.ps1",
        "update_shared_client.ps1",
        "set_client_update_channel.ps1",
        "promote_portable_client.ps1",
        "complete_client_repair.ps1",
    )
    for script_name in script_names:
        (source_scripts / script_name).write_text(
            f"new {script_name}\n",
            encoding="utf-8",
        )
        (target_scripts / script_name).write_text(
            f"partial {script_name}\n",
            encoding="utf-8",
        )

    backup = target / ".erp-client-backup-interrupted"
    backup.mkdir()
    backup_application = backup / "ERP自动化"
    backup_application.mkdir()
    (backup_application / "ERP自动化.exe").write_bytes(b"old-executable")
    (backup / "original-VERSION.txt").write_text(
        "2026.07.30.5\n",
        encoding="utf-8",
    )
    backup_scripts = backup / "scripts"
    backup_scripts.mkdir()
    for script_name in script_names:
        (backup_scripts / script_name).write_text(
            f"old {script_name}\n",
            encoding="utf-8",
        )
    abandoned_stage = target / ".erp-client-promote-interrupted"
    abandoned_stage.mkdir()
    (abandoned_stage / "partial").write_text("partial", encoding="utf-8")
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(tmp_path / "local-appdata")

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
    assert (target / "VERSION.txt").read_text(encoding="utf-8").strip() == (
        "2026.07.30.6"
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_updater_rejects_corrupt_package_before_install(tmp_path: Path) -> None:
    package, manifest_path, version = _build_dummy_release(tmp_path)
    damaged = tmp_path / "damaged.zip"
    damaged.write_bytes(package.read_bytes())
    with damaged.open("r+b") as stream:
        stream.seek(max(0, damaged.stat().st_size // 2))
        original = stream.read(1)
        stream.seek(-1, os.SEEK_CUR)
        stream.write(bytes([original[0] ^ 0xFF]))
    local_appdata = tmp_path / "local-appdata"
    state_root = local_appdata / "LingxingERP"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)

    result = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        "2026.07.24.2",
        "-ManifestFile",
        str(manifest_path),
        "-PackageFile",
        str(damaged),
        "-StateRoot",
        str(state_root),
        "-AssumeYes",
        "-SkipApplicationSmokeTest",
        "-OutputJson",
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "SHA256" in (result.stderr + result.stdout)
    assert not (
        local_appdata / "Programs" / "LingxingERP" / version
    ).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
@pytest.mark.parametrize(
    "unsafe_name",
    [
        "scripts/UPDATE_SHARED_CLIENT.ps1",
        "dist/payload.txt:stream",
        "dist/NUL.txt",
        "dist/trailing-dot.",
    ],
)
def test_updater_rejects_windows_zip_aliases_before_install(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    package, _manifest_path, version = _build_dummy_release(tmp_path)
    unsafe = tmp_path / "unsafe.zip"
    unsafe.write_bytes(package.read_bytes())
    with zipfile.ZipFile(unsafe, "a") as archive:
        archive.writestr(unsafe_name, b"unsafe")
    manifest_path = _write_manifest_for_package(
        tmp_path / "unsafe-manifest.json",
        unsafe,
        version,
        content_sha256="0" * 64,
    )
    local_appdata = tmp_path / "local-appdata"
    state_root = local_appdata / "LingxingERP"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)

    result = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        "2026.07.24.2",
        "-ManifestFile",
        str(manifest_path),
        "-PackageFile",
        str(unsafe),
        "-StateRoot",
        str(state_root),
        "-AssumeYes",
        "-SkipApplicationSmokeTest",
        "-OutputJson",
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "ZIP" in (result.stderr + result.stdout)
    assert not (
        local_appdata / "Programs" / "LingxingERP" / version
    ).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_extracted_tree_is_verified_before_installer_executes(
    tmp_path: Path,
) -> None:
    package, _manifest_path, version = _build_dummy_release(tmp_path)
    manifest_path = _write_manifest_for_package(
        tmp_path / "wrong-content-manifest.json",
        package,
        version,
        content_sha256="0" * 64,
    )
    local_appdata = tmp_path / "local-appdata"
    state_root = local_appdata / "LingxingERP"
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)

    result = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        "2026.07.24.2",
        "-ManifestFile",
        str(manifest_path),
        "-PackageFile",
        str(package),
        "-StateRoot",
        str(state_root),
        "-AssumeYes",
        "-SkipApplicationSmokeTest",
        "-OutputJson",
        env=env,
        check=False,
    )

    assert result.returncode != 0
    assert "安装尚未开始" in (result.stderr + result.stdout)
    assert not (
        local_appdata / "Programs" / "LingxingERP" / version
    ).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_updater_rejects_same_version_release_asset_mutation(
    tmp_path: Path,
) -> None:
    package, manifest_path, version = _build_dummy_release(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state_root = tmp_path / "local-appdata" / "LingxingERP"
    state_root.mkdir(parents=True)
    (state_root / "update-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "last_successful_check_utc": datetime.now(timezone.utc).isoformat(),
                "latest_version": version,
                "package_sha256": manifest["package"]["sha256"],
                "content_sha256": manifest["package"]["content_sha256"],
                "manifest_url": "test",
            }
        ),
        encoding="utf-8",
    )
    manifest["package"]["sha256"] = "1" * 64
    manifest["package"]["content_sha256"] = "2" * 64
    changed_manifest = tmp_path / "changed-manifest.json"
    changed_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        version,
        "-ManifestFile",
        str(changed_manifest),
        "-StateRoot",
        str(state_root),
        "-OutputJson",
        check=False,
    )

    assert result.returncode != 0
    assert "同一版本" in (result.stderr + result.stdout)
    assert hashlib.sha256(package.read_bytes()).hexdigest() != "1" * 64


@pytest.mark.skipif(sys.platform != "win32", reason="Windows updater is required")
def test_updater_rejects_release_manifest_rollback(tmp_path: Path) -> None:
    _package, manifest_path, _version = _build_dummy_release(tmp_path)
    state_root = tmp_path / "local-appdata" / "LingxingERP"
    state_root.mkdir(parents=True)
    (state_root / "update-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "last_successful_check_utc": datetime.now(timezone.utc).isoformat(),
                "latest_version": "2099.01.01.1",
                "package_sha256": "0" * 64,
                "manifest_url": "test",
            }
        ),
        encoding="utf-8",
    )

    result = _run_script(
        ROOT / "scripts" / "update_shared_client.ps1",
        "-CurrentVersion",
        "2026.07.24.2",
        "-ManifestFile",
        str(manifest_path),
        "-StateRoot",
        str(state_root),
        "-OutputJson",
        check=False,
    )

    assert result.returncode != 0
    assert "版本回退" in (result.stderr + result.stdout)
