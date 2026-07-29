from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / "deploy/server/lingxing-erp-cloudflared.service"
DEPLOY_PATH = ROOT / "deploy/server/deploy_current.sh"
DOWNLOAD_PATH = ROOT / "scripts/download_cloudflared.ps1"
TOKEN_PATH = "/etc/lingxing-erp/cloudflare-tunnel-token"
LINUX_SHA256 = "9d71c677db00134c1bd4144b7783486b654ad281b1ea62b4972098d19f770f17"


def _pinned_version() -> str:
    download_script = DOWNLOAD_PATH.read_text(encoding="utf-8")
    match = re.search(r"\[string\]\$Version\s*=\s*'([^']+)'", download_script)
    assert match is not None
    return match.group(1)


def test_cloudflared_unit_uses_root_only_token_file_and_hardened_binary() -> None:
    unit = UNIT_PATH.read_text(encoding="utf-8")

    assert f"ConditionPathExists={TOKEN_PATH}" in unit
    assert "ExecStartPre=/usr/bin/test -x /usr/local/bin/cloudflared" in unit
    assert f"ExecStartPre=/usr/bin/test -s {TOKEN_PATH}" in unit
    assert (
        "ExecStart=/usr/local/bin/cloudflared tunnel --no-autoupdate run "
        f"--token-file {TOKEN_PATH}"
    ) in unit
    assert "--token " not in unit
    assert "TUNNEL_TOKEN=" not in unit
    for directive in (
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    ):
        assert directive in unit


def test_deploy_requires_and_installs_pinned_cloudflared_before_mutations() -> None:
    script = DEPLOY_PATH.read_text(encoding="utf-8")
    version = _pinned_version()

    token_check = script.index(
        "if ! sudo test -s /etc/lingxing-erp/cloudflare-tunnel-token"
    )
    docker_build = script.index("sudo docker build")
    assert token_check < docker_build
    assert "tunnel_token_owner=" in script
    assert "tunnel_token_mode=" in script
    assert '"root:root"' in script
    assert '"600"' in script
    assert f"cloudflared_version={version}" in script
    assert f"cloudflared_sha256={LINUX_SHA256}" in script
    assert (
        "https://github.com/cloudflare/cloudflared/releases/download/"
        "${cloudflared_version}/cloudflared-linux-amd64"
    ) in script
    assert "sha256sum --check --status" in script
    assert "/usr/local/bin/cloudflared" in script
    assert 'sudo sha256sum "${cloudflared_binary}"' in script
    assert (
        '[[ "${installed_cloudflared_sha256}" == "${cloudflared_sha256}" ]]'
        in script
    )
    assert "Reusing verified cloudflared" in script
    assert "docker pull cloudflare/cloudflared" not in script
    assert "lingxing-erp-cloudflared.service" in script
    assert "sudo systemctl enable --now lingxing-erp-cloudflared.service" in script
    assert "sudo systemctl restart lingxing-erp-cloudflared.service" in script
    assert (
        script.index("sudo systemctl restart lingxing-erp-cloudflared.service")
        < script.index(
            "sudo systemctl --no-pager --full status "
            "lingxing-erp-coordinator.service"
        )
    )
    assert (
        "sudo systemctl is-active --quiet "
        "lingxing-erp-cloudflared.service"
    ) in script
