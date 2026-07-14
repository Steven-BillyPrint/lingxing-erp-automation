"""Atomic encrypted local configuration storage with one-generation backup."""

from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path
from typing import Any

from .crypto import LOCAL_CONFIGURATION_PURPOSE, LocalEncryptionBackend, WindowsDpapiBackend
from .errors import ConfigurationDecryptionError, ConfigurationValidationError
from .models import ConfigurationDocument, canonical_json_bytes


LOCAL_ENVELOPE_FORMAT = "erp-automation.local-encrypted-configuration"
LOCAL_ENVELOPE_VERSION = 1
DEFAULT_LOCAL_CONFIG_PATH = Path("config.enc")


def backup_path_for(path: str | Path) -> Path:
    target = Path(path)
    return target.with_name(f"{target.name}.bak")


def _temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")


def _write_private_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: str | Path,
    data: bytes,
    *,
    backup_path: str | Path | None = None,
) -> None:
    """Atomically replace a file and preserve the previous complete generation."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = _temporary_path(target)
    backup = Path(backup_path) if backup_path is not None else None
    backup_temp: Path | None = None
    try:
        _write_private_file(temp, data)
        if backup is not None and target.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup_temp = _temporary_path(backup)
            _write_private_file(backup_temp, target.read_bytes())
            os.replace(backup_temp, backup)
            backup_temp = None
        os.replace(temp, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        _fsync_directory(target.parent)
    finally:
        for leftover in (temp, backup_temp):
            if leftover is not None:
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass


class EncryptedConfigurationStore:
    """Persist a :class:`ConfigurationDocument` in a machine-local envelope."""

    def __init__(
        self,
        path: str | Path = DEFAULT_LOCAL_CONFIG_PATH,
        *,
        backend: LocalEncryptionBackend | None = None,
        backup_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.backup_path = Path(backup_path) if backup_path is not None else backup_path_for(self.path)
        self.backend = backend or WindowsDpapiBackend()

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def encode(self, document: ConfigurationDocument) -> bytes:
        plaintext = canonical_json_bytes(document.to_payload())
        ciphertext = self.backend.encrypt(plaintext, purpose=LOCAL_CONFIGURATION_PURPOSE)
        envelope = {
            "format": LOCAL_ENVELOPE_FORMAT,
            "format_version": LOCAL_ENVELOPE_VERSION,
            "backend": self.backend.name,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        return canonical_json_bytes(envelope)

    def decode(self, encoded: bytes) -> ConfigurationDocument:
        try:
            envelope = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationValidationError("Local encrypted configuration envelope is invalid.") from exc
        if not isinstance(envelope, dict):
            raise ConfigurationValidationError("Local encrypted configuration envelope is invalid.")
        if envelope.get("format") != LOCAL_ENVELOPE_FORMAT:
            raise ConfigurationValidationError("Local encrypted configuration format identifier is invalid.")
        if envelope.get("format_version") != LOCAL_ENVELOPE_VERSION:
            raise ConfigurationValidationError("Local encrypted configuration format version is unsupported.")
        if envelope.get("backend") != self.backend.name:
            raise ConfigurationValidationError(
                "Local encrypted configuration was created by a different encryption backend."
            )
        encoded_ciphertext = envelope.get("ciphertext")
        if not isinstance(encoded_ciphertext, str):
            raise ConfigurationValidationError("Local encrypted configuration ciphertext is invalid.")
        try:
            ciphertext = base64.b64decode(encoded_ciphertext, validate=True)
        except Exception as exc:
            raise ConfigurationValidationError("Local encrypted configuration ciphertext is invalid.") from exc
        try:
            plaintext = self.backend.decrypt(ciphertext, purpose=LOCAL_CONFIGURATION_PURPOSE)
        except ConfigurationDecryptionError:
            raise
        except Exception as exc:
            raise ConfigurationDecryptionError(
                "Local encrypted configuration could not be decrypted or authenticated."
            ) from exc
        try:
            payload: Any = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationValidationError("Decrypted configuration JSON is invalid.") from exc
        return ConfigurationDocument.from_payload(payload)

    def save(self, document: ConfigurationDocument) -> None:
        atomic_write_bytes(
            self.path,
            self.encode(document),
            backup_path=self.backup_path,
        )

    def save_values(self, values: dict[str, Any]) -> ConfigurationDocument:
        document = ConfigurationDocument(values=dict(values))
        self.save(document)
        return document

    def load(self, *, allow_backup_fallback: bool = False) -> ConfigurationDocument:
        try:
            return self.decode(self.path.read_bytes())
        except Exception:
            if not allow_backup_fallback or not self.backup_path.is_file():
                raise
            return self.decode(self.backup_path.read_bytes())

    def load_backup(self) -> ConfigurationDocument:
        if not self.backup_path.is_file():
            raise FileNotFoundError(self.backup_path)
        return self.decode(self.backup_path.read_bytes())

    def restore_backup(self) -> ConfigurationDocument:
        """Validate the backup and restore it without overwriting it with a bad primary."""

        document = self.load_backup()
        atomic_write_bytes(self.path, self.encode(document), backup_path=None)
        return document
