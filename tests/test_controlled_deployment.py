from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "deploy/server/codex_deploy_entry.sh"
GATE = ROOT / "deploy/server/codex_deploy_gate.sh"
INSTALLER = ROOT / "deploy/server/install_codex_deploy_key.sh"
LOCAL_DEPLOY = ROOT / "scripts/deploy_production.ps1"
LOCAL_RELEASE = ROOT / "scripts/publish_client_release.ps1"


def test_shared_key_is_restricted_to_one_server_command() -> None:
    entry = ENTRY.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")

    assert 'SSH_ORIGINAL_COMMAND:-}" != "deploy-main"' in entry
    assert "exec sudo -n /usr/local/sbin/lingxing-codex-deploy" in entry
    assert (
        'restrict,command=\\"/usr/local/sbin/lingxing-codex-deploy-entry\\"'
        in installer
    )
    assert "ssh-ed25519" in installer
    assert "lingxing-codex-controlled-deploy" in installer
    assert "visudo -cf" in installer


def test_server_gate_refuses_active_tasks_and_verifies_health() -> None:
    gate = GATE.read_text(encoding="utf-8")

    assert "flock -n" in gate
    assert "coordination_leases" in gate
    assert "task_id <> '' AND expires_at > ?" in gate
    assert "active background task(s) still hold leases" in gate
    assert 'deploy/server/deploy_current.sh"' in gate
    assert "http://127.0.0.1:18765/health" in gate
    assert "for attempt in $(seq 1 45)" in gate
    assert "did not become ready within 45 seconds" in gate
    assert "required_client_version" in gate
    assert "DEPLOYMENT_HEALTH=healthy" in gate


def test_local_deploy_uses_pinned_host_and_never_allows_password_fallback() -> None:
    script = LOCAL_DEPLOY.read_text(encoding="utf-8")

    assert (
        "Z:\\同事个人\\颜奕超\\ERP自动化部署专用\\codex-production-deploy-ed25519"
        in script
    )
    assert "Copy-Item -LiteralPath $DeployKeyPath -Destination $temporaryKey" in script
    assert "SetAccessRuleProtection($true, $false)" in script
    assert "[Security.AccessControl.FileSystemRights]::FullControl" in script
    assert "Remove-Item -LiteralPath $temporaryKey -Force" in script
    assert "StrictHostKeyChecking=yes" in script
    assert "BatchMode=yes" in script
    assert "IdentitiesOnly=yes" in script
    assert "PasswordAuthentication=no" in script
    assert "KbdInteractiveAuthentication=no" in script
    assert "ssh-keygen -F" in script
    assert "'deploy-main'" in script
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
    for asset in (
        "ERP-Automation-Client.zip",
        "latest.json",
        "SHA256SUMS.txt",
    ):
        assert asset in script
