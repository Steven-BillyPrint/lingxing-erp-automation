"""Encryption backends for local and portable configuration storage."""

from __future__ import annotations

import base64
import ctypes
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .errors import (
    ConfigurationDecryptionError,
    ConfigurationDependencyError,
    ConfigurationPlatformError,
    ConfigurationValidationError,
)


LOCAL_CONFIGURATION_PURPOSE = b"erp-automation/local-configuration/v1"
PORTABLE_MIGRATION_PURPOSE = b"erp-automation/portable-migration/v1"


@runtime_checkable
class LocalEncryptionBackend(Protocol):
    """Machine-local authenticated encryption abstraction."""

    name: str

    def encrypt(self, plaintext: bytes, *, purpose: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes, *, purpose: bytes) -> bytes: ...


@dataclass(frozen=True)
class PortableEncryptedData:
    backend_name: str
    parameters: dict[str, Any]
    ciphertext: bytes


@runtime_checkable
class PortableEncryptionBackend(Protocol):
    """Password-based authenticated encryption abstraction for migration packages."""

    name: str

    def encrypt(
        self,
        plaintext: bytes,
        passphrase: str,
        *,
        purpose: bytes,
    ) -> PortableEncryptedData: ...

    def decrypt(
        self,
        encrypted: PortableEncryptedData,
        passphrase: str,
        *,
        purpose: bytes,
    ) -> bytes: ...


class WindowsDpapiBackend:
    """Protect local configuration with Windows DPAPI for the current user.

    DPAPI ciphertext is deliberately not portable. A portable package must be
    created with :class:`Argon2idAesGcmBackend` before moving to another PC.
    """

    name = "windows-dpapi-current-user"
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def _ensure_windows(self) -> None:
        if os.name != "nt":
            raise ConfigurationPlatformError(
                "Windows DPAPI local configuration is available only on Windows. "
                "Inject a LocalEncryptionBackend for tests on other platforms."
            )

    @classmethod
    def _blob(cls, data: bytes) -> tuple["WindowsDpapiBackend._DataBlob", Any]:
        # Keep the buffer alive for the complete native call.
        size = max(len(data), 1)
        buffer = (ctypes.c_ubyte * size)()
        if data:
            ctypes.memmove(buffer, data, len(data))
        blob = cls._DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        return blob, buffer

    @staticmethod
    def _win_error(prefix: str) -> ConfigurationDecryptionError:
        error_code = ctypes.get_last_error()
        return ConfigurationDecryptionError(f"{prefix} Windows error code: {error_code}.")

    def encrypt(self, plaintext: bytes, *, purpose: bytes) -> bytes:
        self._ensure_windows()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(self._DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(self._DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(self._DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        input_blob, input_buffer = self._blob(plaintext)
        entropy_blob, entropy_buffer = self._blob(purpose)
        output_blob = self._DataBlob()
        # References intentionally retained until after CryptProtectData returns.
        _ = input_buffer, entropy_buffer
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "ERP Automation encrypted configuration",
            ctypes.byref(entropy_blob),
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        if not ok:
            raise self._win_error("DPAPI could not encrypt the local configuration.")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                kernel32.LocalFree(output_blob.pbData)

    def decrypt(self, ciphertext: bytes, *, purpose: bytes) -> bytes:
        self._ensure_windows()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(self._DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(self._DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(self._DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        input_blob, input_buffer = self._blob(ciphertext)
        entropy_blob, entropy_buffer = self._blob(purpose)
        output_blob = self._DataBlob()
        _ = input_buffer, entropy_buffer
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        if not ok:
            raise self._win_error(
                "DPAPI could not decrypt the local configuration for the current Windows user."
            )
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                kernel32.LocalFree(output_blob.pbData)


class Argon2idAesGcmBackend:
    """Portable encryption using Argon2id and AES-256-GCM.

    Imports are lazy so normal DPAPI-only use does not require the portable
    migration dependencies. No key or passphrase is persisted or logged.
    """

    name = "argon2id-aes-256-gcm"

    def __init__(
        self,
        *,
        time_cost: int = 3,
        memory_cost_kib: int = 64 * 1024,
        parallelism: int = 2,
    ) -> None:
        self.time_cost = time_cost
        self.memory_cost_kib = memory_cost_kib
        self.parallelism = parallelism
        self._validate_costs(time_cost, memory_cost_kib, parallelism)

    @staticmethod
    def _validate_costs(time_cost: int, memory_cost_kib: int, parallelism: int) -> None:
        # Bounds protect imports from malicious envelopes that request excessive work.
        if not 1 <= int(time_cost) <= 10:
            raise ConfigurationValidationError("Argon2id time_cost is outside the supported range.")
        if not 8 * 1024 <= int(memory_cost_kib) <= 1024 * 1024:
            raise ConfigurationValidationError("Argon2id memory_cost_kib is outside the supported range.")
        if not 1 <= int(parallelism) <= 16:
            raise ConfigurationValidationError("Argon2id parallelism is outside the supported range.")

    @staticmethod
    def _dependencies():
        try:
            from argon2.low_level import Type, hash_secret_raw
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise ConfigurationDependencyError(
                "Portable migration requires optional dependencies 'argon2-cffi' and "
                "'cryptography'. Install them with: pip install argon2-cffi cryptography"
            ) from exc
        return Type, hash_secret_raw, AESGCM

    @staticmethod
    def _decode_parameter(parameters: dict[str, Any], name: str, expected_length: int) -> bytes:
        value = parameters.get(name)
        if not isinstance(value, str):
            raise ConfigurationValidationError(f"Portable encryption parameter '{name}' is invalid.")
        try:
            decoded = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise ConfigurationValidationError(
                f"Portable encryption parameter '{name}' is invalid."
            ) from exc
        if len(decoded) != expected_length:
            raise ConfigurationValidationError(f"Portable encryption parameter '{name}' is invalid.")
        return decoded

    @classmethod
    def _derive_key(
        cls,
        passphrase: str,
        salt: bytes,
        *,
        time_cost: int,
        memory_cost_kib: int,
        parallelism: int,
    ) -> bytes:
        Type, hash_secret_raw, _AESGCM = cls._dependencies()
        return hash_secret_raw(
            secret=passphrase.encode("utf-8"),
            salt=salt,
            time_cost=time_cost,
            memory_cost=memory_cost_kib,
            parallelism=parallelism,
            hash_len=32,
            type=Type.ID,
            version=19,
        )

    def encrypt(
        self,
        plaintext: bytes,
        passphrase: str,
        *,
        purpose: bytes,
    ) -> PortableEncryptedData:
        _Type, _hash_secret_raw, AESGCM = self._dependencies()
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = self._derive_key(
            passphrase,
            salt,
            time_cost=self.time_cost,
            memory_cost_kib=self.memory_cost_kib,
            parallelism=self.parallelism,
        )
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, purpose)
        return PortableEncryptedData(
            backend_name=self.name,
            parameters={
                "kdf": "argon2id",
                "cipher": "aes-256-gcm",
                "argon2_version": 19,
                "time_cost": self.time_cost,
                "memory_cost_kib": self.memory_cost_kib,
                "parallelism": self.parallelism,
                "key_length": 32,
                "salt": base64.b64encode(salt).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
            },
            ciphertext=ciphertext,
        )

    def decrypt(
        self,
        encrypted: PortableEncryptedData,
        passphrase: str,
        *,
        purpose: bytes,
    ) -> bytes:
        if encrypted.backend_name != self.name:
            raise ConfigurationValidationError("Portable encryption backend identifier is invalid.")
        parameters = encrypted.parameters
        if parameters.get("kdf") != "argon2id" or parameters.get("cipher") != "aes-256-gcm":
            raise ConfigurationValidationError("Portable package does not use Argon2id and AES-256-GCM.")
        if parameters.get("argon2_version") != 19 or parameters.get("key_length") != 32:
            raise ConfigurationValidationError("Portable encryption parameters are unsupported.")
        try:
            time_cost = int(parameters.get("time_cost"))
            memory_cost_kib = int(parameters.get("memory_cost_kib"))
            parallelism = int(parameters.get("parallelism"))
        except (TypeError, ValueError) as exc:
            raise ConfigurationValidationError("Portable Argon2id cost parameters are invalid.") from exc
        self._validate_costs(time_cost, memory_cost_kib, parallelism)
        salt = self._decode_parameter(parameters, "salt", 16)
        nonce = self._decode_parameter(parameters, "nonce", 12)
        _Type, _hash_secret_raw, AESGCM = self._dependencies()
        key = self._derive_key(
            passphrase,
            salt,
            time_cost=time_cost,
            memory_cost_kib=memory_cost_kib,
            parallelism=parallelism,
        )
        try:
            return AESGCM(key).decrypt(nonce, encrypted.ciphertext, purpose)
        except Exception as exc:
            raise ConfigurationDecryptionError(
                "Portable migration authentication failed. The password may be wrong or the package may be damaged."
            ) from exc
