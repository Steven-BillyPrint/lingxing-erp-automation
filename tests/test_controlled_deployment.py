from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "deploy/server/codex_deploy_entry.sh"
GATE = ROOT / "deploy/server/codex_deploy_gate.sh"
SERVER_DEPLOY = ROOT / "deploy/server/deploy_current.sh"
INSTALLER = ROOT / "deploy/server/install_codex_deploy_key.sh"
LOCAL_DEPLOY = ROOT / "scripts/deploy_production.ps1"
LOCAL_RELEASE = ROOT / "scripts/publish_client_release.ps1"
RESTORE_DEPLOY_CREDENTIALS = (
    ROOT / "scripts/restore_production_deploy_credentials.ps1"
)
README = ROOT / "README.md"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def test_shared_key_is_restricted_to_deploy_and_read_only_receipt_commands() -> None:
    entry = ENTRY.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")

    assert 'case "${SSH_ORIGINAL_COMMAND:-}"' in entry
    assert "deploy-main)" in entry
    assert "report-deployed)" in entry
    assert "activate-rollout)" in entry
    assert "--report-deployed" in entry
    assert "--activate-rollout" in entry
    assert "read -r expected_commit expected_version unexpected" in entry
    assert entry.count('expected_version="${expected_version%$\'\\r\'}"') == 2
    assert "exec sudo -n /usr/local/sbin/lingxing-codex-deploy" in entry
    assert '"${expected_commit}"' in entry
    assert '"${expected_version}"' in entry
    assert (
        'restrict,command=\\"/usr/local/sbin/lingxing-codex-deploy-entry\\"'
        in installer
    )
    assert "ssh-ed25519" in installer
    assert "lingxing-codex-controlled-deploy" in installer
    assert "visudo -cf" in installer


def test_server_gate_refuses_active_tasks_and_verifies_health() -> None:
    gate = GATE.read_text(encoding="utf-8")
    deployer = SERVER_DEPLOY.read_text(encoding="utf-8")

    assert "flock -n" in gate
    assert "coordination_leases" in gate
    assert "systemctl is-active --quiet lingxing-erp-coordinator.service" in gate
    assert "task_id <> ''" in gate
    assert "coordinator_active" in gate
    assert "active background task(s) still hold leases" in gate
    assert 'already_deployed=0' in gate
    assert 'if [[ "${already_deployed}" != "1" ]]' in gate
    assert "deployed-main-commit" in gate
    assert 'deploy/server/deploy_current.sh"' in gate
    assert "http://127.0.0.1:18765/health" in gate
    assert "for attempt in $(seq 1 45)" in gate
    assert "did not become ready within 45 seconds" in gate
    assert "required_client_version" in gate
    assert "rollout_previous_client_version" in gate
    assert "client_rollout_grace_remaining_seconds" in gate
    assert "client_rollout_pending_activation" in gate
    assert 'if [[ "${mode}" == "activate" ]]' in gate
    assert "ROLLOUT_ACTIVATED=true" in gate
    assert "ROLLOUT_PENDING=" in gate
    assert "ROLLOUT_DRAIN_ACTIVE=" in gate
    assert "deployment_drain_active" in gate
    assert "recover_pending_rollout" in gate
    assert "pending-mode recovery" in gate
    assert "Rollout activation deferred:" in gate
    assert "active task(s) are draining" in gate
    assert "(4_102_444_800,)" in gate
    assert "now + 60 * 60" not in gate
    assert (
        "Even when existing work delays activation, stop admission of new work"
        in gate
    )
    assert "DEPLOYMENT_HEALTH=healthy" in gate
    assert 'mode=report' in gate
    assert '--report-deployed' in gate
    assert 'if [[ "${mode}" == "report" ]]' in gate
    assert "No verified production deployment receipt exists" in gate
    assert 'expected_commit="$1"' in gate
    assert 'expected_version="$2"' in gate
    assert '"${repository}/deploy/server/deploy_current.sh"' in gate
    assert "client_rollout_grace_deadline_epoch" in deployer

    # The candidate is built while the old service remains available. The
    # final drain and lease count share one database transaction immediately
    # before image promotion, closing the check-then-start race.
    assert "deployment_drain_until" in deployer
    assert "(4_102_444_800,)" in deployer
    assert "now + 60 * 60" not in deployer
    assert 'connection.execute("BEGIN IMMEDIATE")' in deployer
    assert "SELECT COUNT(DISTINCT request_id)" in deployer
    assert "task_id <> '' AND ?" in deployer
    assert (
        '["systemctl", "is-active", "--quiet", '
        '"lingxing-erp-coordinator.service"]'
        in deployer
    )
    assert '["systemctl", "stop", "lingxing-erp-coordinator.service"]' in deployer
    assert "Deployment refused after build" in deployer
    assert 'candidate_image="lingxing-erp-coordinator:candidate-' in deployer
    assert 'rollback_image=lingxing-erp-coordinator:rollback' in deployer
    assert "client-rollout-deadline" in deployer
    assert 'rollout_deadline_epoch="pending"' in deployer
    assert 'rollout_pending_activation=1' in deployer
    assert "service_stop_marker=" in deployer
    assert (
        "service_stop_marker=/etc/lingxing-erp/deployment-in-progress"
        in deployer
    )
    assert (
        "deployment_transaction_root=/etc/lingxing-erp/deploy-rollback"
        in deployer
    )
    assert 'sudo docker tag "${running_image_id}" "${rollback_image}"' in deployer
    assert "previous-image-id" in deployer
    assert "previous-version" in deployer
    assert "rollback image identity changed" in deployer
    assert "previous_service_active" in deployer
    assert "cleanup_deployment_transition" in deployer
    assert "recover_deployment_transaction" in deployer
    assert "restore_interrupted_deployment" in deployer
    assert "committed_deployment_healthy" in deployer
    assert "install_deployment_marker_state committed" in deployer
    assert "Automatic deployment recovery failed" in deployer
    assert "/usr/local/sbin/lingxing-codex-deploy" in deployer
    assert "origin/main moved after release authorization" in deployer
    assert "Authorized commit is already deployed and healthy" in deployer
    assert "deployed-main-commit" in deployer
    assert "verify_restored_coordinator" in deployer
    assert "Restored coordinator did not become healthy within 45 seconds" in deployer
    assert "Restored coordinator version does not match rollback state." in deployer
    assert "restore_optional_rollout_file previous-client-version" in deployer
    assert "restore_optional_rollout_file client-rollout-deadline" in deployer
    assert 'payload.get("client_rollout_pending_activation") is True' in deployer
    assert "rollout_previous_client_version" in deployer

    # Runtime files are backed up before the persistent stop marker is
    # created, and candidate configuration is installed only after the
    # transactional lease check has stopped the old coordinator.
    backup_index = deployer.index("backup_transaction_file coordination-env")
    stop_index = deployer.index(
        '["systemctl", "stop", "lingxing-erp-coordinator.service"]'
    )
    config_install_index = deployer.rindex(
        '"${repository}/deploy/server/coordination.env.example"'
    )
    assert backup_index < stop_index < config_install_index

    # The commit point is durable before the transaction is removed. A pending
    # client rollout deliberately keeps the deployment drain closed until the
    # stable GitHub latest URL has switched and activate-rollout succeeds.
    receipt_index = deployer.rindex(
        'install_rollout_value "${deployed_commit_file}" "${expected_commit}"'
    )
    committed_index = deployer.rindex(
        "install_deployment_marker_state committed"
    )
    clear_index = deployer.rindex("clear_deployment_drain")
    remove_index = deployer.rindex("remove_deployment_transaction")
    assert receipt_index < committed_index < clear_index < remove_index
    assert (
        'if [[ "${rollout_pending_activation}" != "1" ]]'
        in deployer
    )

    for rollback_target in (
        "coordination-env",
        "nas-service",
        "coordinator-service",
        "cloudflared-service",
        "cloudflared-binary",
        "deployed-main-commit",
        "deploy-gate",
        "deploy-entry",
    ):
        assert f"restore_transaction_file {rollback_target}" in deployer


def test_local_deploy_uses_pinned_host_and_never_allows_password_fallback() -> None:
    script = LOCAL_DEPLOY.read_text(encoding="utf-8")

    assert (
        "Codex\\credentials\\erp-production-deploy-ed25519"
        in script
    )
    assert "Codex\\credentials\\erp-production-known_hosts" in script
    assert "Z:\\同事个人\\颜奕超" not in script
    assert "System32\\OpenSSH" in script
    assert "& $sshPath @sshArguments" in script
    assert "& $sshKeygenPath -F" in script
    assert "& ssh @sshArguments" not in script
    assert "'-i', $DeployKeyPath" in script
    assert "Copy-Item -LiteralPath $DeployKeyPath" not in script
    assert "Get-Content -LiteralPath $DeployKeyPath" not in script
    assert "StrictHostKeyChecking=yes" in script
    assert "BatchMode=yes" in script
    assert "IdentitiesOnly=yes" in script
    assert "PasswordAuthentication=no" in script
    assert "KbdInteractiveAuthentication=no" in script
    assert "'deploy-main'" in script
    assert "'report-deployed'" in script
    assert "'activate-rollout'" in script
    assert "Complete-ServerRollout" in script
    assert '$deploymentAuthorization = "$localCommit $version"' in script
    assert "$deployment = Invoke-ControlledDeploymentSsh" in script
    assert "Get-VerifiedDeploymentReceipt" in script
    assert "Compare-ReleaseVersion" in script
    assert "已恢复上一次部署的客户端最新版激活" in script
    assert "不会回退 GitHub 最新版本" in script
    assert "拒绝把服务器或客户端更新通道回退到旧版本" in script
    assert "^DEPLOYED_COMMIT=([0-9a-f]{40})$" in script
    assert "^DEPLOYED_VERSION=" in script
    assert "^ROLLOUT_PENDING=(true|false)$" in script
    assert "^ROLLOUT_DRAIN_ACTIVE=(true|false)$" in script
    assert "RolloutDrainActive" in script
    assert "ROLLOUT_ACTIVATED=true" in script
    assert "DEPLOYMENT_HEALTH=healthy" in script
    assert "ConfirmProductionDeployment" in script


def test_deploy_credential_restore_uses_dpapi_without_exposing_plaintext() -> None:
    script = RESTORE_DEPLOY_CREDENTIALS.read_text(encoding="utf-8")

    assert "ConfirmCredentialRestore" in script
    assert "ProtectedData]::Unprotect" in script
    assert "DataProtectionScope]::CurrentUser" in script
    assert "erp-production-deploy-ed25519.dpapi" in script
    assert "erp-production-deploy-ed25519.pub" in script
    assert "erp-production-known_hosts" in script
    assert "SetAccessRuleProtection($true, $false)" in script
    assert "S-1-5-18" in script
    assert "S-1-5-32-544" in script
    assert "$allowedPrincipals" in script
    assert "WriteAllBytes($temporaryKeyPath, $plainBytes)" in script
    assert "Get-Content -LiteralPath $targetKeyPath" not in script
    assert "Write-Host $plain" not in script

    readme = README.read_text(encoding="utf-8")
    assert "%LOCALAPPDATA%\\Codex\\credentials\\erp-production-deploy-ed25519" in readme
    assert "restore_production_deploy_credentials.ps1 -ConfirmCredentialRestore" in readme
    assert "不保存可直接使用的明文私钥" in readme


def test_release_script_requires_main_and_explicit_confirmation() -> None:
    script = LOCAL_RELEASE.read_text(encoding="utf-8")

    assert "ConfirmProductionRelease" in script
    assert "$branch -ne 'main'" in script
    assert "git status --porcelain --untracked-files=no" in script
    assert "& gh workflow run release.yml" in script
    assert '--field "release_commit=$localCommit"' in script
    assert '--field "ci_run_id=$ciRunId"' in script
    assert '--field "request_id=$releaseRequestId"' in script
    assert "function Get-ReusableCiRun" in script
    assert "--workflow test.yml" in script
    assert "--commit $Commit" in script
    assert "[string]$run.conclusion -ne 'success'" in script
    assert "[string]$_.displayTitle -eq $expectedRunTitle" in script
    assert "gh run watch" in script
    assert "gh release edit $tag --draft=false --latest" in script
    assert "$ErrorActionPreference = 'Continue'" in script
    assert "$releaseViewExitCode = $LASTEXITCODE" in script
    assert "if ($releaseViewExitCode -eq 0)" in script
    assert "function Assert-ReleaseAssets" in script
    assert "gh release download $Tag" in script
    assert "create_release_manifest.ps1" in script
    assert "Release 清单与实际客户端包不一致" in script
    assert "Release SHA256SUMS.txt 与实际客户端包不一致" in script
    assert "拒绝发布低于当前更新通道的版本" in script
    for asset in (
        "ERP-Automation-Client.zip",
        "latest.json",
        "SHA256SUMS.txt",
    ):
        assert asset in script


@pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell required")
@pytest.mark.parametrize(
    (
        "version",
        "channel_version",
        "rollout_pending",
        "rollout_drained",
        "should_activate",
        "should_finalize",
    ),
    [
        ("2099.01.02.1", "2099.01.01.1", True, True, True, True),
        ("2099.01.01.1", "2099.01.02.1", True, True, False, False),
        ("2099.01.01.1", "2099.01.01.1", True, True, False, True),
        # Power can fail after the coordinator loads the absolute rollout
        # deadline but before the durable deployment drain is cleared.
        ("2099.01.01.1", "2099.01.01.1", False, True, False, True),
        ("2099.01.01.1", "2099.01.01.1", False, False, False, False),
    ],
)
def test_deploy_reconciles_server_receipt_without_update_channel_rollback(
    tmp_path: Path,
    version: str,
    channel_version: str,
    rollout_pending: bool,
    rollout_drained: bool,
    should_activate: bool,
    should_finalize: bool,
) -> None:
    command_root = tmp_path / "commands"
    command_root.mkdir()
    command_log = tmp_path / "gh-commands.log"
    activated = tmp_path / "activated"
    rollout_finalized = tmp_path / "rollout-finalized"
    commit = "a" * 40
    key = tmp_path / "deploy-key"
    known_hosts = tmp_path / "known-hosts"
    key.write_text("not-read-by-test\n", encoding="utf-8")
    known_hosts.write_text("pinned\n", encoding="utf-8")

    (command_root / "ssh-keygen.cmd").write_text(
        "@echo off\r\nexit /b 0\r\n",
        encoding="utf-8",
    )
    (command_root / "ssh.cmd").write_text(
        (
            "@echo off\r\n"
            "echo %* | findstr /C:\"activate-rollout\" >nul\r\n"
            "if not errorlevel 1 (\r\n"
            f"  echo finalized>\"{rollout_finalized}\"\r\n"
            f"  echo DEPLOYED_COMMIT={commit}\r\n"
            f"  echo DEPLOYED_VERSION={version}\r\n"
            "  echo ROLLOUT_PENDING=false\r\n"
            "  echo ROLLOUT_DRAIN_ACTIVE=false\r\n"
            "  echo ROLLOUT_ACTIVATED=true\r\n"
            "  echo DEPLOYMENT_HEALTH=healthy\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            f"echo DEPLOYED_COMMIT={commit}\r\n"
            f"echo DEPLOYED_VERSION={version}\r\n"
            f"echo ROLLOUT_PENDING={str(rollout_pending).lower()}\r\n"
            f"echo ROLLOUT_DRAIN_ACTIVE={str(rollout_drained).lower()}\r\n"
            "echo DEPLOYMENT_HEALTH=healthy\r\n"
            "exit /b 0\r\n"
        ),
        encoding="utf-8",
    )
    (command_root / "git.cmd").write_text(
        "@echo off\r\necho feature/not-main\r\nexit /b 0\r\n",
        encoding="utf-8",
    )
    (command_root / "gh.cmd").write_text(
        (
            "@echo off\r\n"
            f"echo %*>>\"{command_log}\"\r\n"
            "if \"%1\"==\"release\" if \"%2\"==\"edit\" (\r\n"
            f"  echo activated>\"{activated}\"\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            f"if \"%1\"==\"release\" if \"%2\"==\"view\" "
            f"if \"%3\"==\"v{version}\" (\r\n"
            "  echo {\"tagName\":\"v"
            f"{version}"
            "\",\"isDraft\":false,\"isPrerelease\":false,"
            f"\"targetCommitish\":\"{commit}\","
            "\"assets\":["
            "{\"name\":\"ERP-Automation-Client.zip\"},"
            "{\"name\":\"latest.json\"},"
            "{\"name\":\"SHA256SUMS.txt\"}],"
            "\"url\":\"https://example.invalid/pending\"}\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            f"if exist \"{activated}\" (\r\n"
            f"  echo {{\"tagName\":\"v{version}\","
            "\"url\":\"https://example.invalid/current\"}\r\n"
            ") else (\r\n"
            f"  echo {{\"tagName\":\"v{channel_version}\","
            "\"url\":\"https://example.invalid/previous\"}\r\n"
            ")\r\n"
            "exit /b 0\r\n"
        ),
        encoding="utf-8",
    )

    environment = dict(os.environ)
    environment["PATH"] = str(command_root) + os.pathsep + environment["PATH"]
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LOCAL_DEPLOY),
            "-ConfirmProductionDeployment",
            "-ServerHost",
            "test.invalid",
            "-DeployKeyPath",
            str(key),
            "-KnownHostsPath",
            str(known_hosts),
            "-SshPath",
            str(command_root / "ssh.cmd"),
            "-SshKeygenPath",
            str(command_root / "ssh-keygen.cmd"),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    # The fake Git checkout intentionally fails the later main-branch gate.
    # Recovery must already have happened from the authenticated server receipt.
    assert result.returncode != 0
    commands = command_log.read_text(encoding="utf-8")
    if should_activate:
        assert activated.is_file()
        assert f"release view v{version}" in commands
        assert f"release edit v{version} --latest" in commands
    else:
        assert not activated.exists()
        assert "release edit" not in commands
    assert rollout_finalized.exists() is should_finalize
