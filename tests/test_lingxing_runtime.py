from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass

import pytest

from erp_automation.configuration import ConfigurationDocument
from erp_automation.configuration.models import canonical_json_bytes
from erp_automation.integrations.lingxing import (
    DpapiTokenStore,
    EncryptedConfigurationCredentialProvider,
    FileInterProcessLock,
    IssuedToken,
    LingxingConfigurationError,
    LingxingCredentials,
    TokenBundle,
    create_lingxing_openapi_client,
)
from erp_automation.integrations.lingxing.runtime import (
    LOCAL_TOKEN_ENVELOPE_FORMAT,
    LOCAL_TOKEN_ENVELOPE_VERSION,
    LOCAL_TOKEN_PAYLOAD_SCHEMA,
    LOCAL_TOKEN_PAYLOAD_VERSION,
    LOCAL_TOKEN_PURPOSE,
    _credentials_fingerprint,
    _token_encryption_purpose,
)


APP_ID = "runtime-test-app-id"
APP_SECRET = "runtime-test-app-secret"
ACCESS_TOKEN = "runtime-test-access-token"
REFRESH_TOKEN = "runtime-test-refresh-token"


class FakeConfigurationStore:
    def __init__(self, values: dict[str, object] | None = None, error: Exception | None = None):
        self.values = values or {}
        self.error = error
        self.allow_backup_fallback: bool | None = None

    def load(self, *, allow_backup_fallback: bool = False) -> ConfigurationDocument:
        self.allow_backup_fallback = allow_backup_fallback
        if self.error is not None:
            raise self.error
        return ConfigurationDocument(values=dict(self.values))


class XorLocalBackend:
    name = "test-xor-local"

    def __init__(self, key: int = 0xA7) -> None:
        self.key = key
        self.purposes: list[bytes] = []

    def encrypt(self, plaintext: bytes, *, purpose: bytes) -> bytes:
        self.purposes.append(purpose)
        return bytes(value ^ self.key for value in plaintext)

    def decrypt(self, ciphertext: bytes, *, purpose: bytes) -> bytes:
        self.purposes.append(purpose)
        return bytes(value ^ self.key for value in ciphertext)


class FakeHTTPClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, object]]] = []
        self.closed = False

    async def request(self, method: str, url: str, **kwargs):
        self.requests.append((method, url, kwargs))
        raise AssertionError("factory wiring test must not make a network request")

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FakeTokenEndpoint:
    issue_count: int = 0

    async def issue_token(self, credentials: LingxingCredentials) -> IssuedToken:
        self.issue_count += 1
        assert credentials.app_id == APP_ID
        assert credentials.app_secret == APP_SECRET
        return IssuedToken(ACCESS_TOKEN, REFRESH_TOKEN, 3600)

    async def refresh_token(self, app_id: str, refresh_token: str) -> IssuedToken:
        raise AssertionError("no refresh is expected")


def _token() -> TokenBundle:
    return TokenBundle(
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
        issued_at=1_700_000_000,
        expires_at=1_700_003_600,
        refresh_expires_at=1_700_007_200,
        generation=3,
    )


def test_credential_provider_reads_nested_canonical_keys_without_leaking() -> None:
    store = FakeConfigurationStore(
        {"lingxing": {"app_id": APP_ID, "app_secret": APP_SECRET}}
    )
    provider = EncryptedConfigurationCredentialProvider(store)

    credentials = provider.get_credentials()

    assert credentials.app_id == APP_ID
    assert credentials.app_secret == APP_SECRET
    assert store.allow_backup_fallback is True
    assert APP_SECRET not in repr(provider)
    assert APP_SECRET not in repr(credentials)


@pytest.mark.parametrize(
    "values",
    [
        {"LINGXING_APP_ID": APP_ID, "LINGXING_APP_SECRET": APP_SECRET},
        {"LINGXING_APPID": APP_ID, "LINGXING_APPSECRET": APP_SECRET},
        {"LINGXING": {"APP_ID": APP_ID, "APP_SECRET": APP_SECRET}},
        {"lingxing.app_id": APP_ID, "lingxing.app_secret": APP_SECRET},
    ],
)
def test_credential_provider_accepts_reasonable_legacy_key_forms(values) -> None:
    credentials = EncryptedConfigurationCredentialProvider(
        FakeConfigurationStore(values)
    ).get_credentials()

    assert (credentials.app_id, credentials.app_secret) == (APP_ID, APP_SECRET)


def test_credential_provider_reports_actionable_missing_config() -> None:
    provider = EncryptedConfigurationCredentialProvider(
        FakeConfigurationStore({"lingxing.app_id": APP_ID})
    )

    with pytest.raises(LingxingConfigurationError) as captured:
        provider.get_credentials()

    message = str(captured.value)
    assert "lingxing.app_id" in message
    assert "lingxing.app_secret" in message
    assert APP_ID not in message


def test_credential_provider_redacts_untrusted_backend_exception() -> None:
    provider = EncryptedConfigurationCredentialProvider(
        FakeConfigurationStore(error=RuntimeError(f"could not decode {APP_SECRET}"))
    )

    with pytest.raises(LingxingConfigurationError) as captured:
        provider.get_credentials()

    assert APP_SECRET not in str(captured.value)
    assert APP_SECRET not in repr(captured.value)


def test_dpapi_token_store_round_trip_is_atomic_encrypted_and_clearable(tmp_path) -> None:
    async def run() -> None:
        backend = XorLocalBackend()
        path = tmp_path / "local-only-token.enc"
        store = DpapiTokenStore(
            path,
            app_id=APP_ID,
            app_secret=APP_SECRET,
            backend=backend,
        )

        assert await store.load() is None
        await store.save(_token())
        encoded = path.read_bytes()
        assert ACCESS_TOKEN.encode() not in encoded
        assert REFRESH_TOKEN.encode() not in encoded
        envelope = json.loads(encoded.decode("utf-8"))
        assert envelope["format"] == LOCAL_TOKEN_ENVELOPE_FORMAT
        assert envelope["format_version"] == LOCAL_TOKEN_ENVELOPE_VERSION
        assert envelope["backend"] == backend.name
        fingerprint = _credentials_fingerprint(APP_ID, APP_SECRET)
        assert envelope["credentials_fingerprint"] == fingerprint
        assert envelope["credentials_fingerprint"] not in {APP_ID, APP_SECRET}
        assert APP_ID.encode() not in encoded
        assert APP_SECRET.encode() not in encoded
        assert not path.with_name(f"{path.name}.bak").exists()

        loaded = await store.load()
        assert loaded == _token()
        expected_purpose = _token_encryption_purpose(fingerprint)
        assert backend.purposes == [expected_purpose, expected_purpose]
        assert ACCESS_TOKEN not in repr(store)
        assert REFRESH_TOKEN not in repr(store)

        await store.clear()
        assert not path.exists()
        assert await store.load() is None

    asyncio.run(run())


def test_dpapi_token_store_invalidates_unbound_v1_token_safely(tmp_path) -> None:
    async def run() -> None:
        backend = XorLocalBackend()
        legacy_payload = {
            "schema": LOCAL_TOKEN_PAYLOAD_SCHEMA,
            "schema_version": 1,
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "issued_at": 1_700_000_000,
            "expires_at": 1_700_003_600,
            "refresh_expires_at": 1_700_007_200,
            "generation": 3,
        }
        ciphertext = backend.encrypt(
            canonical_json_bytes(legacy_payload),
            purpose=LOCAL_TOKEN_PURPOSE,
        )
        legacy_envelope = {
            "format": LOCAL_TOKEN_ENVELOPE_FORMAT,
            "format_version": 1,
            "backend": backend.name,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        path = tmp_path / "legacy-token.enc"
        path.write_bytes(canonical_json_bytes(legacy_envelope))

        store = DpapiTokenStore(
            path,
            app_id=APP_ID,
            app_secret=APP_SECRET,
            backend=backend,
        )

        assert await store.load() is None
        assert path.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("changed_app_id", "changed_app_secret"),
    [
        ("runtime-test-changed-app-id", APP_SECRET),
        (APP_ID, "runtime-test-changed-app-secret"),
    ],
)
def test_factory_does_not_reuse_token_after_credentials_change(
    tmp_path,
    changed_app_id: str,
    changed_app_secret: str,
) -> None:
    async def run() -> None:
        changed_access_token = "runtime-test-changed-access-token"
        changed_refresh_token = "runtime-test-changed-refresh-token"
        backend = XorLocalBackend()
        path = tmp_path / "token.enc"
        await DpapiTokenStore(
            path,
            app_id=APP_ID,
            app_secret=APP_SECRET,
            backend=backend,
        ).save(_token())

        @dataclass
        class ChangedCredentialEndpoint:
            issue_count: int = 0
            refresh_count: int = 0

            async def issue_token(self, credentials: LingxingCredentials) -> IssuedToken:
                self.issue_count += 1
                assert credentials.app_id == changed_app_id
                assert credentials.app_secret == changed_app_secret
                return IssuedToken(
                    changed_access_token,
                    changed_refresh_token,
                    3600,
                )

            async def refresh_token(self, app_id: str, refresh_token: str) -> IssuedToken:
                self.refresh_count += 1
                raise AssertionError("another credential pair's refresh token must not be reused")

        endpoint = ChangedCredentialEndpoint()
        client = await create_lingxing_openapi_client(
            FakeConfigurationStore(
                {
                    "lingxing.app_id": changed_app_id,
                    "lingxing.app_secret": changed_app_secret,
                }
            ),  # type: ignore[arg-type]
            token_path=path,
            token_backend=backend,
            lock_path=tmp_path / "token.lock",
            http_client=FakeHTTPClient(),
            token_endpoint=endpoint,
            clock=lambda: 1_700_000_000,
        )

        token = await client._token_manager.get_token()

        assert token.access_token == changed_access_token
        assert endpoint.issue_count == 1
        assert endpoint.refresh_count == 0
        encoded = path.read_bytes()
        assert APP_ID.encode() not in encoded
        assert changed_app_id.encode() not in encoded
        assert APP_SECRET.encode() not in encoded
        assert changed_app_secret.encode() not in encoded
        envelope = json.loads(encoded.decode("utf-8"))
        assert envelope["credentials_fingerprint"] == _credentials_fingerprint(
            changed_app_id,
            changed_app_secret,
        )

        await client.aclose()

    asyncio.run(run())


def test_dpapi_token_store_rejects_invalid_expiry_without_leaking(tmp_path) -> None:
    async def run() -> None:
        backend = XorLocalBackend()
        fingerprint = _credentials_fingerprint(APP_ID, APP_SECRET)
        payload = {
            "schema": LOCAL_TOKEN_PAYLOAD_SCHEMA,
            "schema_version": LOCAL_TOKEN_PAYLOAD_VERSION,
            "credentials_fingerprint": fingerprint,
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "issued_at": 200,
            "expires_at": 100,
            "refresh_expires_at": 300,
            "generation": 1,
        }
        ciphertext = backend.encrypt(
            canonical_json_bytes(payload),
            purpose=_token_encryption_purpose(fingerprint),
        )
        envelope = {
            "format": LOCAL_TOKEN_ENVELOPE_FORMAT,
            "format_version": LOCAL_TOKEN_ENVELOPE_VERSION,
            "backend": backend.name,
            "credentials_fingerprint": fingerprint,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        path = tmp_path / "bad-token.enc"
        path.write_bytes(canonical_json_bytes(envelope))
        store = DpapiTokenStore(
            path,
            app_id=APP_ID,
            app_secret=APP_SECRET,
            backend=backend,
        )

        with pytest.raises(LingxingConfigurationError) as captured:
            await store.load()

        assert "清除" in str(captured.value)
        assert ACCESS_TOKEN not in str(captured.value)
        assert REFRESH_TOKEN not in repr(captured.value)

    asyncio.run(run())


def test_dpapi_token_store_rejects_invalid_bundle_before_writing(tmp_path) -> None:
    async def run() -> None:
        path = tmp_path / "token.enc"
        store = DpapiTokenStore(
            path,
            app_id=APP_ID,
            app_secret=APP_SECRET,
            backend=XorLocalBackend(),
        )
        invalid = TokenBundle(
            access_token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            issued_at=100,
            expires_at=100,
            refresh_expires_at=200,
        )

        with pytest.raises(LingxingConfigurationError) as captured:
            await store.save(invalid)

        assert not path.exists()
        assert ACCESS_TOKEN not in str(captured.value)

    asyncio.run(run())


def test_factory_wires_config_dpapi_token_store_and_file_lock_without_network(tmp_path) -> None:
    async def run() -> None:
        config_store = FakeConfigurationStore(
            {"lingxing.app_id": APP_ID, "lingxing.app_secret": APP_SECRET}
        )
        backend = XorLocalBackend()
        http = FakeHTTPClient()
        endpoint = FakeTokenEndpoint()
        token_path = tmp_path / "tokens" / "token.enc"
        lock_path = tmp_path / "locks" / "token.lock"

        client = await create_lingxing_openapi_client(
            config_store,  # type: ignore[arg-type]
            token_path=token_path,
            token_backend=backend,
            lock_path=lock_path,
            http_client=http,
            token_endpoint=endpoint,
            clock=lambda: 1_700_000_000,
        )

        manager = client._token_manager
        assert isinstance(manager._token_store, DpapiTokenStore)
        assert manager._token_store.path == token_path
        assert isinstance(manager._interprocess_lock, FileInterProcessLock)
        assert manager._interprocess_lock.path == lock_path

        token = await manager.get_token()
        assert token.access_token == ACCESS_TOKEN
        assert endpoint.issue_count == 1
        assert token_path.exists()
        assert lock_path.exists()
        assert http.requests == []

        await client.aclose()
        assert http.closed is False

    asyncio.run(run())


def test_factory_fails_before_transport_use_when_configuration_is_missing(tmp_path) -> None:
    async def run() -> None:
        http = FakeHTTPClient()

        with pytest.raises(LingxingConfigurationError) as captured:
            await create_lingxing_openapi_client(
                FakeConfigurationStore({}),  # type: ignore[arg-type]
                token_path=tmp_path / "token.enc",
                token_backend=XorLocalBackend(),
                lock_path=tmp_path / "token.lock",
                http_client=http,
                token_endpoint=FakeTokenEndpoint(),
            )

        assert "lingxing.app_id" in str(captured.value)
        assert http.requests == []
        assert http.closed is False

    asyncio.run(run())
