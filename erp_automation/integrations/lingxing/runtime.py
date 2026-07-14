"""Production wiring for encrypted Lingxing credentials and local tokens.

This module is the boundary between the generic OpenAPI client and the
application's encrypted configuration.  Long-lived tokens deliberately use a
separate, current-Windows-user DPAPI envelope.  The default token location is
outside the workspace so it cannot be included in a portable migration
package.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from erp_automation.configuration import (
    DEFAULT_LOCAL_CONFIG_PATH,
    ConfigurationDependencyError,
    ConfigurationDocument,
    ConfigurationError,
    ConfigurationPlatformError,
    EncryptedConfigurationStore,
    LocalEncryptionBackend,
    WindowsDpapiBackend,
    atomic_write_bytes,
)
from erp_automation.configuration.models import canonical_json_bytes

from .auth import (
    CredentialProvider,
    FileInterProcessLock,
    LingxingCredentials,
    TokenBundle,
    TokenEndpoint,
    TokenManager,
    TokenStore,
)
from .client import (
    DEFAULT_BASE_URL,
    AsyncHTTPClient,
    LingxingOpenAPIClient,
    LingxingTokenEndpoint,
)
from .errors import LingxingConfigurationError


LOCAL_TOKEN_PURPOSE = b"erp-automation/lingxing-local-token/v1"
LOCAL_TOKEN_ENVELOPE_FORMAT = "erp-automation.lingxing-local-token"
LOCAL_TOKEN_ENVELOPE_VERSION = 2
LOCAL_TOKEN_PAYLOAD_SCHEMA = "erp-automation.lingxing-token-bundle"
LOCAL_TOKEN_PAYLOAD_VERSION = 2
_CREDENTIALS_FINGERPRINT_DOMAIN = (
    b"erp-automation/lingxing-credentials-fingerprint/v1\0"
)


def default_local_state_directory() -> Path:
    """Return a per-user, non-portable directory for runtime-only state."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ERPAutomation"
    # This fallback mainly makes dependency-injected tests and diagnostics
    # deterministic on non-Windows hosts.  The production backend still gives
    # an actionable error if DPAPI itself is unavailable.
    return Path.home() / ".erp-automation" / "local"


DEFAULT_LOCAL_TOKEN_PATH = default_local_state_directory() / "lingxing-token.enc"
DEFAULT_LOCAL_TOKEN_LOCK_PATH = default_local_state_directory() / "lingxing-token.lock"


class ConfigurationStoreReader(Protocol):
    """Small read-only surface used by the credential provider."""

    def load(self, *, allow_backup_fallback: bool = False) -> ConfigurationDocument: ...


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _non_empty_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalized_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in values.items():
        normalized = _normalized_key(key)
        if normalized and normalized not in output:
            output[normalized] = value
    return output


def _find_lingxing_value(values: Mapping[str, Any], field: str) -> str | None:
    """Read canonical keys plus case/underscore-compatible legacy keys."""

    normalized_values = _normalized_mapping(values)
    section = normalized_values.get("lingxing")
    if isinstance(section, Mapping):
        section_value = _normalized_mapping(section).get(_normalized_key(field))
        text = _non_empty_text(section_value)
        if text is not None:
            return text

    # Normalization intentionally makes these equivalent:
    # lingxing.app_id, LINGXING_APP_ID and LINGXING_APPID.
    return _non_empty_text(normalized_values.get(_normalized_key(f"lingxing.{field}")))


class EncryptedConfigurationCredentialProvider(CredentialProvider):
    """Load Lingxing credentials from an encrypted configuration document.

    The provider never includes configuration values in its representation or
    errors.  A client should be recreated after the user changes its AppID so
    signing and token issuance use the same immutable credential snapshot.
    """

    def __init__(
        self,
        store: ConfigurationStoreReader,
        *,
        allow_backup_fallback: bool = True,
    ) -> None:
        self._store = store
        self._allow_backup_fallback = bool(allow_backup_fallback)

    def get_credentials(self) -> LingxingCredentials:
        try:
            document = self._store.load(
                allow_backup_fallback=self._allow_backup_fallback,
            )
        except FileNotFoundError:
            raise LingxingConfigurationError(
                "未找到加密配置文件。请先在程序的“设置”中填写并保存领星 OpenAPI 配置。"
            ) from None
        except ConfigurationPlatformError:
            raise LingxingConfigurationError(
                "当前系统无法读取 Windows DPAPI 加密配置；请在创建该配置的 Windows 用户下运行。"
            ) from None
        except ConfigurationDependencyError:
            raise LingxingConfigurationError(
                "读取加密配置所需的安全组件未安装；请重新安装项目依赖后再试。"
            ) from None
        except (ConfigurationError, OSError):
            raise LingxingConfigurationError(
                "加密配置无法读取或已损坏。请在“设置”中重新保存，或恢复 config.enc.bak。"
            ) from None
        except Exception:
            # A custom/injected backend must not be able to leak plaintext
            # through an exception message.
            raise LingxingConfigurationError(
                "读取加密配置失败。请检查配置文件和当前 Windows 用户。"
            ) from None

        if not isinstance(document, ConfigurationDocument):
            raise LingxingConfigurationError(
                "加密配置格式不正确。请在程序的“设置”中重新保存配置。"
            )

        app_id = _find_lingxing_value(document.values, "app_id")
        app_secret = _find_lingxing_value(document.values, "app_secret")
        if app_id is None or app_secret is None:
            raise LingxingConfigurationError(
                "领星 OpenAPI 配置不完整：请填写 lingxing.app_id 和 lingxing.app_secret 后保存。"
            )
        return LingxingCredentials(app_id=app_id, app_secret=app_secret)

    def __repr__(self) -> str:
        return (
            "EncryptedConfigurationCredentialProvider("
            f"allow_backup_fallback={self._allow_backup_fallback}, values=<redacted>)"
        )


def _validated_number(payload: Mapping[str, Any], field: str) -> float:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {field}")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"invalid {field}")
    return normalized


def _credentials_fingerprint(app_id: str, app_secret: str) -> str:
    """Return a non-reversible identifier for one complete credential pair."""

    if not isinstance(app_id, str) or not app_id.strip():
        raise LingxingConfigurationError("领星 AppID 为空，无法安全绑定本机令牌。")
    if not isinstance(app_secret, str) or not app_secret:
        raise LingxingConfigurationError("领星 AppSecret 为空，无法安全绑定本机令牌。")
    digest = hashlib.sha256(_CREDENTIALS_FINGERPRINT_DOMAIN)
    # Length-prefix both values so no delimiter or concatenation ambiguity is
    # possible.  Neither plaintext value is retained after this constructor.
    for value in (app_id, app_secret):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _validated_credentials_fingerprint(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("invalid credentials fingerprint")
    if any(character not in "0123456789abcdef" for character in value):
        raise ValueError("invalid credentials fingerprint")
    return value


def _token_encryption_purpose(credentials_fingerprint: str) -> bytes:
    fingerprint = _validated_credentials_fingerprint(credentials_fingerprint)
    # DPAPI uses ``purpose`` as optional entropy, so copying an envelope and
    # changing only its visible fingerprint cannot rebind the ciphertext.
    return LOCAL_TOKEN_PURPOSE + b"/credentials-sha256/" + fingerprint.encode("ascii")


class _TokenBindingMismatch(ValueError):
    """Internal signal: the cached token belongs to other credentials."""


def _token_to_payload(
    token: TokenBundle,
    *,
    credentials_fingerprint: str,
) -> dict[str, Any]:
    if not isinstance(token, TokenBundle):
        raise ValueError("invalid token object")
    access_token = _non_empty_text(token.access_token)
    refresh_token = _non_empty_text(token.refresh_token)
    if access_token is None or refresh_token is None:
        raise ValueError("empty token")
    issued_at = _validated_number({"value": token.issued_at}, "value")
    expires_at = _validated_number({"value": token.expires_at}, "value")
    refresh_expires_at = _validated_number(
        {"value": token.refresh_expires_at},
        "value",
    )
    if expires_at <= issued_at or refresh_expires_at <= issued_at:
        raise ValueError("invalid expiry order")
    if isinstance(token.generation, bool) or not isinstance(token.generation, int):
        raise ValueError("invalid generation")
    if token.generation < 1:
        raise ValueError("invalid generation")
    fingerprint = _validated_credentials_fingerprint(credentials_fingerprint)
    return {
        "schema": LOCAL_TOKEN_PAYLOAD_SCHEMA,
        "schema_version": LOCAL_TOKEN_PAYLOAD_VERSION,
        "credentials_fingerprint": fingerprint,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "refresh_expires_at": refresh_expires_at,
        "generation": token.generation,
    }


def _token_from_payload(
    payload: object,
    *,
    expected_credentials_fingerprint: str,
) -> TokenBundle:
    if not isinstance(payload, dict):
        raise ValueError("invalid token payload")
    if payload.get("schema") != LOCAL_TOKEN_PAYLOAD_SCHEMA:
        raise ValueError("invalid token schema")
    if payload.get("schema_version") != LOCAL_TOKEN_PAYLOAD_VERSION:
        raise ValueError("invalid token version")
    fingerprint = _validated_credentials_fingerprint(
        payload.get("credentials_fingerprint")
    )
    expected = _validated_credentials_fingerprint(
        expected_credentials_fingerprint
    )
    if not hmac.compare_digest(fingerprint, expected):
        raise _TokenBindingMismatch("token belongs to other credentials")
    access_token = _non_empty_text(payload.get("access_token"))
    refresh_token = _non_empty_text(payload.get("refresh_token"))
    if access_token is None or refresh_token is None:
        raise ValueError("empty token")
    issued_at = _validated_number(payload, "issued_at")
    expires_at = _validated_number(payload, "expires_at")
    refresh_expires_at = _validated_number(payload, "refresh_expires_at")
    if expires_at <= issued_at or refresh_expires_at <= issued_at:
        raise ValueError("invalid expiry order")
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("invalid generation")
    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        issued_at=issued_at,
        expires_at=expires_at,
        refresh_expires_at=refresh_expires_at,
        generation=generation,
    )


class DpapiTokenStore(TokenStore):
    """Atomic, machine-local encrypted persistence for rotating API tokens."""

    def __init__(
        self,
        path: str | Path = DEFAULT_LOCAL_TOKEN_PATH,
        *,
        app_id: str,
        app_secret: str,
        backend: LocalEncryptionBackend | None = None,
    ) -> None:
        self.path = Path(path)
        self.backend = backend or WindowsDpapiBackend()
        # Retain only a one-way fingerprint.  Neither credential plaintext may
        # be written into the token file or exposed through this store.
        self._credentials_fingerprint = _credentials_fingerprint(
            app_id,
            app_secret,
        )
        self._encryption_purpose = _token_encryption_purpose(
            self._credentials_fingerprint
        )

    async def load(self) -> TokenBundle | None:
        return await asyncio.to_thread(self._load_sync)

    async def save(self, token: TokenBundle) -> None:
        await asyncio.to_thread(self._save_sync, token)

    async def clear(self) -> None:
        await asyncio.to_thread(self._clear_sync)

    def _load_sync(self) -> TokenBundle | None:
        if not self.path.exists():
            return None
        try:
            encoded = self.path.read_bytes()
            envelope = json.loads(encoded.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("invalid envelope")
            if envelope.get("format") != LOCAL_TOKEN_ENVELOPE_FORMAT:
                raise ValueError("invalid envelope format")
            format_version = envelope.get("format_version")
            if (
                isinstance(format_version, int)
                and not isinstance(format_version, bool)
                and 1 <= format_version < LOCAL_TOKEN_ENVELOPE_VERSION
            ):
                # Version 1 had no credential binding.  It must never be reused
                # after credentials change; TokenManager will issue a new pair
                # and atomically replace this file.
                return None
            if format_version != LOCAL_TOKEN_ENVELOPE_VERSION:
                raise ValueError("invalid envelope version")
            if envelope.get("backend") != self.backend.name:
                raise ValueError("invalid envelope backend")
            envelope_fingerprint = _validated_credentials_fingerprint(
                envelope.get("credentials_fingerprint")
            )
            if not hmac.compare_digest(
                envelope_fingerprint,
                self._credentials_fingerprint,
            ):
                return None
            ciphertext_text = envelope.get("ciphertext")
            if not isinstance(ciphertext_text, str):
                raise ValueError("invalid ciphertext")
            ciphertext = base64.b64decode(ciphertext_text, validate=True)
            plaintext = self.backend.decrypt(
                ciphertext,
                purpose=self._encryption_purpose,
            )
            payload = json.loads(plaintext.decode("utf-8"))
            return _token_from_payload(
                payload,
                expected_credentials_fingerprint=self._credentials_fingerprint,
            )
        except _TokenBindingMismatch:
            return None
        except ConfigurationPlatformError:
            raise LingxingConfigurationError(
                "领星令牌只能由保存它的 Windows 用户解密；请清除本机令牌后重新登录授权。"
            ) from None
        except ConfigurationDependencyError:
            raise LingxingConfigurationError(
                "缺少本机令牌加密组件；请重新安装项目依赖。"
            ) from None
        except Exception:
            raise LingxingConfigurationError(
                "本机领星令牌文件无法读取、已损坏或不属于当前 Windows 用户；请清除后重新生成。"
            ) from None

    def _save_sync(self, token: TokenBundle) -> None:
        try:
            plaintext = canonical_json_bytes(
                _token_to_payload(
                    token,
                    credentials_fingerprint=self._credentials_fingerprint,
                )
            )
            ciphertext = self.backend.encrypt(
                plaintext,
                purpose=self._encryption_purpose,
            )
            envelope = {
                "format": LOCAL_TOKEN_ENVELOPE_FORMAT,
                "format_version": LOCAL_TOKEN_ENVELOPE_VERSION,
                "backend": self.backend.name,
                "credentials_fingerprint": self._credentials_fingerprint,
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            }
            # Do not keep a previous rotating refresh token as a backup: it may
            # already have been consumed.  os.replace still makes this atomic.
            atomic_write_bytes(self.path, canonical_json_bytes(envelope))
        except ConfigurationPlatformError:
            raise LingxingConfigurationError(
                "当前系统无法使用 Windows DPAPI 保存领星令牌。"
            ) from None
        except ConfigurationDependencyError:
            raise LingxingConfigurationError(
                "缺少本机令牌加密组件；请重新安装项目依赖。"
            ) from None
        except Exception:
            raise LingxingConfigurationError(
                "无法安全保存本机领星令牌；请检查本机数据目录是否可写。"
            ) from None

    def _clear_sync(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise LingxingConfigurationError(
                "无法清除本机领星令牌；请关闭其他程序并检查文件权限。"
            ) from None

    def __repr__(self) -> str:
        return f"DpapiTokenStore(path={str(self.path)!r}, token=<redacted>)"


async def _resolve_credentials(
    provider: CredentialProvider,
) -> LingxingCredentials:
    value = provider.get_credentials()
    credentials = await value if inspect.isawaitable(value) else value
    if not isinstance(credentials, LingxingCredentials):
        raise LingxingConfigurationError("凭据提供器返回了无效结果，请重新保存领星配置。")
    credentials.validate()
    return credentials


async def create_lingxing_openapi_client(
    configuration_store: EncryptedConfigurationStore | None = None,
    *,
    configuration_path: str | Path = DEFAULT_LOCAL_CONFIG_PATH,
    configuration_backend: LocalEncryptionBackend | None = None,
    token_path: str | Path = DEFAULT_LOCAL_TOKEN_PATH,
    token_backend: LocalEncryptionBackend | None = None,
    lock_path: str | Path = DEFAULT_LOCAL_TOKEN_LOCK_PATH,
    lock_timeout: float = 30.0,
    http_client: AsyncHTTPClient | None = None,
    token_endpoint: TokenEndpoint | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 30.0,
    max_read_retries: int = 2,
    retry_base_delay: float = 0.25,
    refresh_skew_seconds: float = 600.0,
    refresh_token_lifetime_seconds: float = 7200.0,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> LingxingOpenAPIClient:
    """Build the production client with encrypted config, tokens and locking.

    ``http_client`` and both encryption backends are injectable so unit tests do
    not use the network or Windows DPAPI.  When omitted, an owned ``httpx``
    client and current-user DPAPI are used.
    """

    if configuration_store is None:
        configuration_store = EncryptedConfigurationStore(
            configuration_path,
            backend=configuration_backend,
        )
    provider = EncryptedConfigurationCredentialProvider(configuration_store)
    credentials = await _resolve_credentials(provider)
    # Pin one credential snapshot for this client.  Recreate the client after a
    # settings change so the signer and token endpoint cannot use different IDs.
    pinned_provider = _PinnedCredentialProvider(credentials)
    token_store = DpapiTokenStore(
        token_path,
        app_id=credentials.app_id,
        app_secret=credentials.app_secret,
        backend=token_backend,
    )
    interprocess_lock = FileInterProcessLock(lock_path, timeout=lock_timeout)

    owns_http_client = False
    transport = http_client
    if transport is None:
        try:
            import httpx
        except ImportError:
            raise LingxingConfigurationError(
                "缺少 httpx，无法连接领星 OpenAPI；请执行 python -m pip install -r requirements.txt。"
            ) from None
        transport = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        owns_http_client = True

    effective_clock = clock
    try:
        endpoint = token_endpoint or LingxingTokenEndpoint(transport, base_url=base_url)
        manager_kwargs: dict[str, Any] = {
            "refresh_skew_seconds": refresh_skew_seconds,
            "refresh_token_lifetime_seconds": refresh_token_lifetime_seconds,
        }
        if effective_clock is not None:
            manager_kwargs["clock"] = effective_clock
        manager = TokenManager(
            pinned_provider,
            token_store,
            interprocess_lock,
            endpoint,
            **manager_kwargs,
        )
        client_kwargs: dict[str, Any] = {
            "app_id": credentials.app_id,
            "base_url": base_url,
            "timeout": timeout,
            "max_read_retries": max_read_retries,
            "retry_base_delay": retry_base_delay,
            "sleeper": sleeper,
            "owns_http_client": owns_http_client,
        }
        if effective_clock is not None:
            client_kwargs["clock"] = effective_clock
        return LingxingOpenAPIClient(transport, manager, **client_kwargs)
    except Exception:
        if owns_http_client:
            await transport.aclose()
        raise


@dataclass(frozen=True)
class _PinnedCredentialProvider(CredentialProvider):
    credentials: LingxingCredentials

    def get_credentials(self) -> LingxingCredentials:
        return self.credentials

    def __repr__(self) -> str:
        return "_PinnedCredentialProvider(credentials=<redacted>)"


# Concise compatibility aliases for application wiring.
ConfigurationCredentialProvider = EncryptedConfigurationCredentialProvider
LocalEncryptedTokenStore = DpapiTokenStore


__all__ = [
    "ConfigurationCredentialProvider",
    "DEFAULT_LOCAL_TOKEN_LOCK_PATH",
    "DEFAULT_LOCAL_TOKEN_PATH",
    "DpapiTokenStore",
    "EncryptedConfigurationCredentialProvider",
    "LOCAL_TOKEN_ENVELOPE_FORMAT",
    "LOCAL_TOKEN_ENVELOPE_VERSION",
    "LOCAL_TOKEN_PURPOSE",
    "LocalEncryptedTokenStore",
    "create_lingxing_openapi_client",
    "default_local_state_directory",
]
