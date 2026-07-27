"""Password-protected, portable authorization for the shared desktop client."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from erp_automation.configuration import (
    Argon2idAesGcmBackend,
    ConfigurationDecryptionError,
    MigrationValidationError,
    PortableEncryptedData,
    atomic_write_bytes,
    backup_path_for,
)
from erp_automation.configuration.models import canonical_json_bytes


CLIENT_ACCESS_PROFILE_FORMAT = "erp-automation.client-access-profile"
CLIENT_ACCESS_PROFILE_VERSION = 1
CLIENT_ACCESS_PROFILE_PURPOSE = b"erp-automation/client-access-profile/v1"
CLIENT_ACCESS_PROFILE_SUFFIX = ".erp-client"
_MAX_PROFILE_BYTES = 8 * 1024 * 1024
_MAX_CREDENTIAL_BYTES = 256 * 1024
_MAX_CONFIGURATION_PACKAGE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ClientAccessProfile:
    server_host: str
    server_user: str
    ssh_private_key: bytes
    known_hosts: bytes
    coordination_token: str
    configuration_package: bytes = b""


def _validate_passphrase(passphrase: str) -> None:
    if len(str(passphrase or "")) < 12:
        raise MigrationValidationError(
            "客户端授权文件密码必须至少包含 12 个字符。"
        )


def _read_limited(path: Path, *, label: str) -> bytes:
    if not path.is_file():
        raise MigrationValidationError(f"{label}不存在：{path}")
    size = path.stat().st_size
    if size <= 0 or size > _MAX_CREDENTIAL_BYTES:
        raise MigrationValidationError(f"{label}大小无效。")
    return path.read_bytes()


def _validated_profile(payload: object) -> ClientAccessProfile:
    if not isinstance(payload, dict):
        raise MigrationValidationError("客户端授权内容必须是 JSON 对象。")
    if payload.get("format") != CLIENT_ACCESS_PROFILE_FORMAT:
        raise MigrationValidationError("客户端授权内容格式无效。")
    if payload.get("format_version") != CLIENT_ACCESS_PROFILE_VERSION:
        raise MigrationValidationError("客户端授权内容版本不受支持。")

    def decode_bytes(name: str, *, maximum: int) -> bytes:
        value = payload.get(name)
        if not isinstance(value, str):
            raise MigrationValidationError(f"客户端授权字段 {name} 无效。")
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise MigrationValidationError(
                f"客户端授权字段 {name} 无效。"
            ) from exc
        if len(decoded) > maximum:
            raise MigrationValidationError(f"客户端授权字段 {name} 过大。")
        return decoded

    server_host = str(payload.get("server_host") or "").strip()
    server_user = str(payload.get("server_user") or "").strip()
    private_key = decode_bytes(
        "ssh_private_key",
        maximum=_MAX_CREDENTIAL_BYTES,
    )
    known_hosts = decode_bytes(
        "known_hosts",
        maximum=_MAX_CREDENTIAL_BYTES,
    )
    configuration_package = decode_bytes(
        "configuration_package",
        maximum=_MAX_CONFIGURATION_PACKAGE_BYTES,
    )
    token = str(payload.get("coordination_token") or "").strip()
    if not server_host or not server_user:
        raise MigrationValidationError("客户端授权文件缺少服务器地址或用户。")
    if not private_key.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
        raise MigrationValidationError("客户端授权文件中的 SSH 私钥无效。")
    if not known_hosts.strip():
        raise MigrationValidationError("客户端授权文件中的主机指纹为空。")
    if len(token) < 32 or len(token) > 4096:
        raise MigrationValidationError("客户端授权文件中的协调服务凭据无效。")
    return ClientAccessProfile(
        server_host=server_host,
        server_user=server_user,
        ssh_private_key=private_key,
        known_hosts=known_hosts,
        coordination_token=token,
        configuration_package=configuration_package,
    )


def export_client_access_profile(
    destination: str | Path,
    passphrase: str,
    *,
    state_root: str | Path,
    server_host: str,
    server_user: str,
    configuration_package: bytes = b"",
) -> Path:
    """Export local connection authorization without placing it in the release."""

    _validate_passphrase(passphrase)
    root = Path(state_root)
    package = bytes(configuration_package)
    if len(package) > _MAX_CONFIGURATION_PACKAGE_BYTES:
        raise MigrationValidationError("加密设置包过大，无法写入客户端授权文件。")
    payload = {
        "format": CLIENT_ACCESS_PROFILE_FORMAT,
        "format_version": CLIENT_ACCESS_PROFILE_VERSION,
        "server_host": str(server_host or "").strip(),
        "server_user": str(server_user or "").strip(),
        "ssh_private_key": base64.b64encode(
            _read_limited(
                root / "server-tunnel-ed25519",
                label="SSH 私钥",
            )
        ).decode("ascii"),
        "known_hosts": base64.b64encode(
            _read_limited(root / "known_hosts", label="服务器主机指纹")
        ).decode("ascii"),
        "coordination_token": (
            _read_limited(
                root / "coordination-token",
                label="协调服务凭据",
            )
            .decode("utf-8-sig")
            .strip()
        ),
        "configuration_package": base64.b64encode(package).decode("ascii"),
    }
    profile = _validated_profile(payload)
    del profile
    backend = Argon2idAesGcmBackend()
    encrypted = backend.encrypt(
        canonical_json_bytes(payload),
        passphrase,
        purpose=CLIENT_ACCESS_PROFILE_PURPOSE,
    )
    envelope = {
        "format": CLIENT_ACCESS_PROFILE_FORMAT,
        "format_version": CLIENT_ACCESS_PROFILE_VERSION,
        "backend": encrypted.backend_name,
        "parameters": encrypted.parameters,
        "ciphertext": base64.b64encode(encrypted.ciphertext).decode("ascii"),
    }
    target = Path(destination)
    atomic_write_bytes(
        target,
        canonical_json_bytes(envelope),
        backup_path=backup_path_for(target),
    )
    return target


def load_client_access_profile(
    source: str | Path,
    passphrase: str,
) -> ClientAccessProfile:
    """Decrypt and authenticate a portable client authorization file."""

    _validate_passphrase(passphrase)
    path = Path(source)
    if not path.is_file() or path.stat().st_size > _MAX_PROFILE_BYTES:
        raise MigrationValidationError("客户端授权文件不存在或过大。")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationValidationError("客户端授权文件无法读取。") from exc
    if not isinstance(envelope, dict):
        raise MigrationValidationError("客户端授权文件格式无效。")
    if envelope.get("format") != CLIENT_ACCESS_PROFILE_FORMAT:
        raise MigrationValidationError("客户端授权文件格式无效。")
    if envelope.get("format_version") != CLIENT_ACCESS_PROFILE_VERSION:
        raise MigrationValidationError("客户端授权文件版本不受支持。")
    backend = Argon2idAesGcmBackend()
    if envelope.get("backend") != backend.name:
        raise MigrationValidationError("客户端授权文件加密算法不受支持。")
    parameters = envelope.get("parameters")
    encoded = envelope.get("ciphertext")
    if not isinstance(parameters, dict) or not isinstance(encoded, str):
        raise MigrationValidationError("客户端授权文件密文无效。")
    try:
        ciphertext = base64.b64decode(encoded, validate=True)
        plaintext = backend.decrypt(
            PortableEncryptedData(
                backend_name=backend.name,
                parameters=parameters,
                ciphertext=ciphertext,
            ),
            passphrase,
            purpose=CLIENT_ACCESS_PROFILE_PURPOSE,
        )
    except ConfigurationDecryptionError:
        raise
    except Exception as exc:
        raise ConfigurationDecryptionError(
            "客户端授权文件认证失败；密码错误或文件已损坏。"
        ) from exc
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationValidationError("客户端授权内容无效。") from exc
    return _validated_profile(payload)


def install_client_access_profile(
    profile: ClientAccessProfile,
    *,
    state_root: str | Path,
    expected_server_host: str,
    expected_server_user: str,
) -> None:
    """Install an authenticated profile for the current Windows user."""

    validate_client_access_profile_identity(
        profile,
        expected_server_host=expected_server_host,
        expected_server_user=expected_server_user,
    )
    install_client_access_files(
        state_root=state_root,
        ssh_private_key=profile.ssh_private_key,
        known_hosts=profile.known_hosts,
        coordination_token=profile.coordination_token,
    )


def validate_client_access_profile_identity(
    profile: ClientAccessProfile,
    *,
    expected_server_host: str,
    expected_server_user: str,
) -> None:
    """Reject an authenticated profile intended for another server."""

    if profile.server_host != str(expected_server_host or "").strip():
        raise MigrationValidationError("授权文件的服务器地址与当前客户端不一致。")
    if profile.server_user != str(expected_server_user or "").strip():
        raise MigrationValidationError("授权文件的服务器用户与当前客户端不一致。")


def install_client_access_files(
    *,
    state_root: str | Path,
    ssh_private_key: bytes,
    known_hosts: bytes,
    coordination_token: str,
) -> None:
    """Validate and atomically install manually supplied authorization."""

    profile = _validated_profile(
        {
            "format": CLIENT_ACCESS_PROFILE_FORMAT,
            "format_version": CLIENT_ACCESS_PROFILE_VERSION,
            "server_host": "manual",
            "server_user": "manual",
            "ssh_private_key": base64.b64encode(bytes(ssh_private_key)).decode(
                "ascii"
            ),
            "known_hosts": base64.b64encode(bytes(known_hosts)).decode("ascii"),
            "coordination_token": str(coordination_token or "").strip(),
            "configuration_package": "",
        }
    )
    root = Path(state_root)
    atomic_write_bytes(
        root / "server-tunnel-ed25519",
        profile.ssh_private_key,
        backup_path=backup_path_for(root / "server-tunnel-ed25519"),
    )
    atomic_write_bytes(
        root / "known_hosts",
        profile.known_hosts,
        backup_path=backup_path_for(root / "known_hosts"),
    )
    atomic_write_bytes(
        root / "coordination-token",
        (profile.coordination_token + "\n").encode("utf-8"),
        backup_path=backup_path_for(root / "coordination-token"),
    )


__all__ = [
    "CLIENT_ACCESS_PROFILE_SUFFIX",
    "ClientAccessProfile",
    "export_client_access_profile",
    "install_client_access_files",
    "install_client_access_profile",
    "load_client_access_profile",
    "validate_client_access_profile_identity",
]
