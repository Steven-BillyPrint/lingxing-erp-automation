from __future__ import annotations

from pathlib import Path

import pytest

from erp_automation.configuration import (
    ConfigurationDecryptionError,
    MigrationValidationError,
)
from erp_automation.coordination.access_profile import (
    export_client_access_profile,
    install_client_access_files,
    install_client_access_profile,
    load_client_access_profile,
)


PRIVATE_KEY = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
    b"dGVzdC1wcml2YXRlLWtleQ==\n"
    b"-----END OPENSSH PRIVATE KEY-----\n"
)
KNOWN_HOSTS = b"example.test ssh-ed25519 dGVzdC1ob3N0LWtleQ==\n"
TOKEN = "portable-client-token-" + ("x" * 32)
PASSPHRASE = "correct horse battery staple"


def _write_access_files(state_root: Path) -> None:
    state_root.mkdir(parents=True)
    (state_root / "server-tunnel-ed25519").write_bytes(PRIVATE_KEY)
    (state_root / "known_hosts").write_bytes(KNOWN_HOSTS)
    (state_root / "coordination-token").write_text(TOKEN + "\n", encoding="utf-8")


def test_portable_client_profile_is_encrypted_and_installs_on_another_host(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    _write_access_files(source_root)
    configuration_package = b"encrypted-server-configuration-package"
    destination = tmp_path / "ERP-client.erp-client"

    export_client_access_profile(
        destination,
        PASSPHRASE,
        state_root=source_root,
        server_host="8.133.172.100",
        server_user="admin",
        configuration_package=configuration_package,
    )

    encrypted_bytes = destination.read_bytes()
    assert PRIVATE_KEY not in encrypted_bytes
    assert KNOWN_HOSTS.strip() not in encrypted_bytes
    assert TOKEN.encode() not in encrypted_bytes
    assert configuration_package not in encrypted_bytes

    with pytest.raises(ConfigurationDecryptionError):
        load_client_access_profile(destination, "this password is wrong")

    profile = load_client_access_profile(destination, PASSPHRASE)
    assert profile.configuration_package == configuration_package
    install_client_access_profile(
        profile,
        state_root=target_root,
        expected_server_host="8.133.172.100",
        expected_server_user="admin",
    )
    assert (target_root / "server-tunnel-ed25519").read_bytes() == PRIVATE_KEY
    assert (target_root / "known_hosts").read_bytes() == KNOWN_HOSTS
    assert (
        (target_root / "coordination-token").read_text(encoding="utf-8").strip()
        == TOKEN
    )


def test_client_profile_rejects_a_different_server_and_invalid_manual_key(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _write_access_files(source_root)
    destination = tmp_path / "ERP-client.erp-client"
    export_client_access_profile(
        destination,
        PASSPHRASE,
        state_root=source_root,
        server_host="8.133.172.100",
        server_user="admin",
    )
    profile = load_client_access_profile(destination, PASSPHRASE)

    with pytest.raises(MigrationValidationError):
        install_client_access_profile(
            profile,
            state_root=tmp_path / "wrong-server",
            expected_server_host="example.invalid",
            expected_server_user="admin",
        )
    with pytest.raises(MigrationValidationError):
        install_client_access_files(
            state_root=tmp_path / "manual",
            ssh_private_key=b"not-an-openssh-private-key",
            known_hosts=KNOWN_HOSTS,
            coordination_token=TOKEN,
        )
