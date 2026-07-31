from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "deploy/server/codex_deploy_entry.sh"
GATE = ROOT / "deploy/server/codex_deploy_gate.sh"
SERVER_DEPLOY = ROOT / "deploy/server/deploy_current.sh"
INSTALLER = ROOT / "deploy/server/install_codex_deploy_key.sh"
LOCAL_DEPLOY = ROOT / "scripts/deploy_production.ps1"
LOCAL_RELEASE = ROOT / "scripts/publish_client_release.ps1"


def test_shared_key_is_restricted_to_one_server_command() -> None:
    entry = ENTRY.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")

    assert 'SSH_ORIGINAL_COMMAND:-}" != "deploy-main"' in entry
    assert "read -r expected_commit expected_version unexpected" in entry
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
    assert "task_id <> '' AND expires_at > ?" in gate
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
    assert "DEPLOYMENT_HEALTH=healthy" in gate
    assert 'expected_commit="$1"' in gate
    assert 'expected_version="$2"' in gate
    assert '"${repository}/deploy/server/deploy_current.sh"' in gate
    assert "client_rollout_grace_deadline_epoch" in deployer

    # The candidate is built while the old service remains available. The
    # final drain and lease count share one database transaction immediately
    # before image promotion, closing the check-then-start race.
    assert "deployment_drain_until" in deployer
    assert 'connection.execute("BEGIN IMMEDIATE")' in deployer
    assert "SELECT COUNT(DISTINCT request_id)" in deployer
    assert '["systemctl", "stop", "lingxing-erp-coordinator.service"]' in deployer
    assert "Deployment refused after build" in deployer
    assert 'candidate_image="lingxing-erp-coordinator:candidate-' in deployer
    assert 'rollback_image=lingxing-erp-coordinator:rollback' in deployer
    assert "client-rollout-deadline" in deployer
    assert "service_stop_marker=" in deployer
    assert 'sudo docker tag "${running_image_id}" "${rollback_image}"' in deployer
    assert "previous_service_active" in deployer
    assert "cleanup_deployment_transition" in deployer
    assert "/usr/local/sbin/lingxing-codex-deploy" in deployer
    assert "origin/main moved after release authorization" in deployer
    assert "Authorized commit is already deployed and healthy" in deployer
    assert "deployed-main-commit" in deployer


def test_local_deploy_uses_pinned_host_and_never_allows_password_fallback() -> None:
    script = LOCAL_DEPLOY.read_text(encoding="utf-8")

    assert (
        "Z:\\同事个人\\颜奕超\\ERP自动化部署专用\\codex-production-deploy-ed25519"
        in script
    )
    assert "'-i', $DeployKeyPath" in script
    assert "Copy-Item -LiteralPath $DeployKeyPath" not in script
    assert "Get-Content -LiteralPath $DeployKeyPath" not in script
    assert "StrictHostKeyChecking=yes" in script
    assert "BatchMode=yes" in script
    assert "IdentitiesOnly=yes" in script
    assert "PasswordAuthentication=no" in script
    assert "KbdInteractiveAuthentication=no" in script
    assert "ssh-keygen -F" in script
    assert "'deploy-main'" in script
    assert '$deploymentAuthorization = "$localCommit $version"' in script
    assert "$deploymentAuthorization |" in script
    assert "^DEPLOYED_COMMIT=([0-9a-f]{40})$" in script
    assert "^DEPLOYED_VERSION=" in script
    assert "DEPLOYMENT_HEALTH=healthy" in script
    assert "ConfirmProductionDeployment" in script


def test_release_script_requires_main_and_explicit_confirmation() -> None:
    script = LOCAL_RELEASE.read_text(encoding="utf-8")

    assert "ConfirmProductionRelease" in script
    assert "$branch -ne 'main'" in script
    assert "git status --porcelain --untracked-files=no" in script
    assert "gh workflow run release.yml --ref main" in script
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
    for asset in (
        "ERP-Automation-Client.zip",
        "latest.json",
        "SHA256SUMS.txt",
    ):
        assert asset in script
