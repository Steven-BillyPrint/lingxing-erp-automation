from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path

import pytest

from erp_automation.configuration import (
    Argon2idAesGcmBackend,
    ConfigurationDecryptionError,
    ConfigurationDependencyError,
    ConfigurationDocument,
    ConfigurationPlatformError,
    ConfigurationValidationError,
    EncryptedConfigurationStore,
    MigrationImportError,
    MigrationPathSpec,
    MigrationScope,
    MigrationValidationError,
    PortableEncryptedData,
    PortableMigrationService,
    WindowsDpapiBackend,
    default_migration_path_specs,
    import_env_file,
    parse_env_file,
)
from erp_automation.configuration.crypto import (
    LOCAL_CONFIGURATION_PURPOSE,
    PORTABLE_MIGRATION_PURPOSE,
)


class FakeLocalEncryptionBackend:
    """Authenticated test backend; production code never selects this backend."""

    name = "test-local-authenticated"

    def __init__(self, key: bytes = b"test-machine-key") -> None:
        self.key = key

    def _stream(self, purpose: bytes, nonce: bytes) -> bytes:
        return hashlib.sha256(self.key + purpose + nonce).digest()

    def encrypt(self, plaintext: bytes, *, purpose: bytes) -> bytes:
        nonce = os.urandom(16)
        stream = self._stream(purpose, nonce)
        ciphertext = bytes(value ^ stream[index % len(stream)] for index, value in enumerate(plaintext))
        tag = hmac.new(self.key, purpose + nonce + ciphertext, hashlib.sha256).digest()
        return nonce + tag + ciphertext

    def decrypt(self, ciphertext: bytes, *, purpose: bytes) -> bytes:
        if len(ciphertext) < 48:
            raise ConfigurationDecryptionError("Test ciphertext authentication failed.")
        nonce, tag, body = ciphertext[:16], ciphertext[16:48], ciphertext[48:]
        expected = hmac.new(self.key, purpose + nonce + body, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise ConfigurationDecryptionError("Test ciphertext authentication failed.")
        stream = self._stream(purpose, nonce)
        return bytes(value ^ stream[index % len(stream)] for index, value in enumerate(body))


class FakePortableEncryptionBackend:
    """Injectable authenticated backend that keeps tests independent of optional wheels."""

    name = "test-portable-authenticated"

    @staticmethod
    def _key(passphrase: str, purpose: bytes) -> bytes:
        return hashlib.sha256(passphrase.encode("utf-8") + purpose).digest()

    def encrypt(
        self,
        plaintext: bytes,
        passphrase: str,
        *,
        purpose: bytes,
    ) -> PortableEncryptedData:
        key = self._key(passphrase, purpose)
        nonce = os.urandom(16)
        stream = hashlib.sha256(key + nonce).digest()
        ciphertext = bytes(value ^ stream[index % len(stream)] for index, value in enumerate(plaintext))
        tag = hmac.new(key, purpose + nonce + ciphertext, hashlib.sha256).digest()
        return PortableEncryptedData(
            backend_name=self.name,
            parameters={
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "tag": base64.b64encode(tag).decode("ascii"),
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
        try:
            nonce = base64.b64decode(encrypted.parameters["nonce"], validate=True)
            tag = base64.b64decode(encrypted.parameters["tag"], validate=True)
        except Exception as exc:
            raise ConfigurationDecryptionError("Test package authentication failed.") from exc
        key = self._key(passphrase, purpose)
        expected = hmac.new(key, purpose + nonce + encrypted.ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise ConfigurationDecryptionError("Test package authentication failed.")
        stream = hashlib.sha256(key + nonce).digest()
        return bytes(
            value ^ stream[index % len(stream)]
            for index, value in enumerate(encrypted.ciphertext)
        )


PASSPHRASE = "correct horse battery staple"


def _local_store(path: Path, *, key: bytes = b"test-machine-key") -> EncryptedConfigurationStore:
    return EncryptedConfigurationStore(path, backend=FakeLocalEncryptionBackend(key))


def _portable_service() -> PortableMigrationService:
    return PortableMigrationService(backend=FakePortableEncryptionBackend())


def test_configuration_document_is_versioned_and_repr_is_redacted():
    document = ConfigurationDocument(
        values={"LINGXING_PASSWORD": "top-secret", "feature": {"enabled": True}},
    )

    payload = document.to_payload()
    restored = ConfigurationDocument.from_payload(payload)

    assert payload["schema"] == "erp-automation.configuration"
    assert payload["schema_version"] == 1
    assert restored.values == document.values
    assert "top-secret" not in repr(document)
    assert "<redacted>" in repr(document)

    payload["schema_version"] = 99
    with pytest.raises(ConfigurationValidationError, match="Unsupported configuration schema version"):
        ConfigurationDocument.from_payload(payload)


def test_local_encrypted_store_round_trip_backup_and_explicit_recovery(tmp_path):
    config_path = tmp_path / "config.enc"
    store = _local_store(config_path)
    first = ConfigurationDocument(values={"TOKEN": "first-secret"})
    second = ConfigurationDocument(values={"TOKEN": "second-secret"})

    store.save(first)
    assert store.load().values == first.values
    assert b"first-secret" not in config_path.read_bytes()

    store.save(second)
    assert store.load().values == second.values
    assert store.load_backup().values == first.values
    assert config_path.with_name("config.enc.bak").is_file()

    config_path.write_bytes(b"damaged")
    with pytest.raises(ConfigurationValidationError):
        store.load()
    assert store.load(allow_backup_fallback=True).values == first.values
    assert store.restore_backup().values == first.values
    assert store.load().values == first.values


def test_local_store_rejects_a_different_machine_backend(tmp_path):
    path = tmp_path / "config.enc"
    _local_store(path, key=b"machine-a").save_values({"TOKEN": "secret"})

    with pytest.raises(ConfigurationDecryptionError):
        _local_store(path, key=b"machine-b").load()


def test_env_import_parses_quotes_filters_keys_and_never_returns_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\ufeff# local credentials\n"
        "LINGXING_ACCOUNT=user@example.com\n"
        "LINGXING_PASSWORD='do-not-print'\n"
        'export ALIBABA_PASSWORD="another-secret" # comment\n'
        "KEEP_EXISTING=new-value\n",
        encoding="utf-8",
    )
    store = _local_store(tmp_path / "config.enc")
    store.save_values({"KEEP_EXISTING": "old-value"})

    parsed = parse_env_file(env_path)
    result = import_env_file(
        store,
        env_path,
        include_keys={"LINGXING_ACCOUNT", "LINGXING_PASSWORD", "KEEP_EXISTING"},
        overwrite=False,
    )

    assert parsed["ALIBABA_PASSWORD"] == "another-secret"
    assert store.load().values == {
        "KEEP_EXISTING": "old-value",
        "LINGXING_ACCOUNT": "user@example.com",
        "LINGXING_PASSWORD": "do-not-print",
    }
    assert result.imported_count == 2
    assert result.skipped_count == 2
    assert "do-not-print" not in repr(result)
    assert "another-secret" not in repr(result)
    assert b"do-not-print" not in store.path.read_bytes()


def test_invalid_env_error_reports_only_the_line_number(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("NOT AN ASSIGNMENT containing-secret", encoding="utf-8")

    with pytest.raises(ConfigurationValidationError) as captured:
        parse_env_file(env_path)

    assert "line 1" in str(captured.value)
    assert "containing-secret" not in str(captured.value)


def test_windows_dpapi_has_a_clear_non_windows_error():
    if os.name == "nt":
        pytest.skip("Non-Windows guard is not applicable on Windows.")

    with pytest.raises(ConfigurationPlatformError, match="only on Windows"):
        WindowsDpapiBackend().encrypt(b"data", purpose=LOCAL_CONFIGURATION_PURPOSE)


def test_production_portable_backend_uses_argon2id_and_aes_gcm_or_has_clear_dependency_error():
    has_dependencies = bool(importlib.util.find_spec("argon2")) and bool(
        importlib.util.find_spec("cryptography")
    )
    backend = Argon2idAesGcmBackend(time_cost=1, memory_cost_kib=8 * 1024, parallelism=1)
    if not has_dependencies:
        with pytest.raises(ConfigurationDependencyError) as captured:
            backend.encrypt(b"payload", PASSPHRASE, purpose=PORTABLE_MIGRATION_PURPOSE)
        assert "argon2-cffi" in str(captured.value)
        assert "cryptography" in str(captured.value)
        assert PASSPHRASE not in str(captured.value)
        return

    encrypted = backend.encrypt(b"payload", PASSPHRASE, purpose=PORTABLE_MIGRATION_PURPOSE)
    assert encrypted.parameters["kdf"] == "argon2id"
    assert encrypted.parameters["cipher"] == "aes-256-gcm"
    assert encrypted.parameters["key_length"] == 32
    assert backend.decrypt(encrypted, PASSPHRASE, purpose=PORTABLE_MIGRATION_PURPOSE) == b"payload"


def test_configuration_only_portable_round_trip_and_reencrypts_for_destination(tmp_path):
    service = _portable_service()
    package_path = tmp_path / "settings.erp-migration"
    document = ConfigurationDocument(values={"API_SECRET": "portable-secret", "enabled": True})

    manifest = service.export_package(document, package_path, PASSPHRASE)
    validated = service.validate_package(package_path, PASSPHRASE)

    assert manifest.scope is MigrationScope.CONFIGURATION_ONLY
    assert manifest.files == ()
    assert validated.configuration.values == document.values
    assert validated.files == {}
    assert b"portable-secret" not in package_path.read_bytes()
    assert "portable-secret" not in repr(validated)

    destination_store = _local_store(tmp_path / "destination" / "config.enc", key=b"new-machine")
    result = service.import_package(
        package_path,
        PASSPHRASE,
        config_store=destination_store,
    )
    assert destination_store.load().values == document.values
    assert result.scope is MigrationScope.CONFIGURATION_ONLY
    assert result.imported_file_count == 0
    assert "portable-secret" not in repr(result)


def test_portable_package_wrong_password_and_tampering_fail_authentication(tmp_path):
    service = _portable_service()
    package_path = tmp_path / "settings.erp-migration"
    service.export_package(ConfigurationDocument(values={"TOKEN": "secret"}), package_path, PASSPHRASE)

    with pytest.raises(ConfigurationDecryptionError) as wrong_password:
        service.validate_package(package_path, "a different long password")
    assert PASSPHRASE not in str(wrong_password.value)

    envelope = json.loads(package_path.read_text(encoding="utf-8"))
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
    ciphertext[len(ciphertext) // 2] ^= 1
    envelope["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
    package_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ConfigurationDecryptionError):
        service.validate_package(package_path, PASSPHRASE)


def test_full_migration_manifest_checksums_import_and_backups(tmp_path):
    workspace = tmp_path / "source"
    (workspace / "data").mkdir(parents=True)
    (workspace / "rules").mkdir()
    (workspace / "data" / "automation.sqlite3").write_bytes(b"sqlite-state")
    (workspace / "rules" / "custom.json").write_text('{"rule":1}', encoding="utf-8")
    (workspace / ".env").write_text("PASSWORD=raw-secret", encoding="utf-8")
    package_path = tmp_path / "full.erp-migration"
    service = _portable_service()
    specs = (
        MigrationPathSpec("data/automation.sqlite3", required=True),
        MigrationPathSpec("rules", required=True),
    )

    manifest = service.export_package(
        ConfigurationDocument(values={"PASSWORD": "encrypted-secret"}),
        package_path,
        PASSPHRASE,
        scope=MigrationScope.FULL,
        workspace_root=workspace,
        path_specs=specs,
    )
    validated = service.validate_package(package_path, PASSPHRASE)

    assert [entry.path for entry in manifest.files] == ["data/automation.sqlite3", "rules/custom.json"]
    assert validated.files["data/automation.sqlite3"] == b"sqlite-state"
    assert ".env" not in validated.files
    assert b"encrypted-secret" not in package_path.read_bytes()
    assert b"complete" not in package_path.read_bytes()
    assert any("browser_profile" in note for note in manifest.notes)

    destination = tmp_path / "destination"
    (destination / "data").mkdir(parents=True)
    (destination / "data" / "automation.sqlite3").write_text("old-state", encoding="utf-8")
    destination_store = _local_store(destination / "config.enc", key=b"destination-machine")

    with pytest.raises(MigrationImportError, match="already exists"):
        service.import_package(
            package_path,
            PASSPHRASE,
            config_store=destination_store,
            destination_root=destination,
        )
    assert not destination_store.exists
    assert (destination / "data" / "automation.sqlite3").read_text(encoding="utf-8") == "old-state"

    result = service.import_package(
        package_path,
        PASSPHRASE,
        config_store=destination_store,
        destination_root=destination,
        overwrite=True,
    )
    assert result.imported_paths == ("data/automation.sqlite3", "rules/custom.json")
    assert destination_store.load().values == {"PASSWORD": "encrypted-secret"}
    assert (destination / "data" / "automation.sqlite3").read_bytes() == b"sqlite-state"
    assert (destination / "data" / "automation.sqlite3.bak").read_text(encoding="utf-8") == "old-state"
    assert (destination / "rules" / "custom.json").read_text(encoding="utf-8") == '{"rule":1}'


@pytest.mark.parametrize(
    "unsafe_path",
    ["../outside.txt", ".env", "config.enc", "browser_profile/Cookies"],
)
def test_full_export_rejects_nonportable_or_unsafe_paths(tmp_path, unsafe_path):
    workspace = tmp_path / "source"
    workspace.mkdir()
    if ".." not in unsafe_path:
        target = workspace.joinpath(*unsafe_path.replace("\\", "/").split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("sensitive", encoding="utf-8")

    with pytest.raises(MigrationValidationError):
        _portable_service().export_package(
            ConfigurationDocument(values={"TOKEN": "secret"}),
            tmp_path / "bad.erp-migration",
            PASSPHRASE,
            scope=MigrationScope.FULL,
            workspace_root=workspace,
            path_specs=(MigrationPathSpec(unsafe_path, required=True),),
        )


def test_configuration_only_export_rejects_workspace_file_specs(tmp_path):
    with pytest.raises(MigrationValidationError, match="may not include workspace paths"):
        _portable_service().export_package(
            ConfigurationDocument(values={}),
            tmp_path / "bad.erp-migration",
            PASSPHRASE,
            scope=MigrationScope.CONFIGURATION_ONLY,
            path_specs=(MigrationPathSpec("data/file.json"),),
        )


def test_inner_manifest_checksum_mismatch_is_rejected_before_import(tmp_path):
    service = _portable_service()
    backend = service.backend
    workspace = tmp_path / "source"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "automation.sqlite3").write_text("original", encoding="utf-8")
    package_path = tmp_path / "full.erp-migration"
    service.export_package(
        ConfigurationDocument(values={"TOKEN": "secret"}),
        package_path,
        PASSPHRASE,
        scope=MigrationScope.FULL,
        workspace_root=workspace,
        path_specs=(MigrationPathSpec("data/automation.sqlite3", required=True),),
    )

    envelope = json.loads(package_path.read_text(encoding="utf-8"))
    encrypted = PortableEncryptedData(
        backend_name=backend.name,
        parameters=envelope["parameters"],
        ciphertext=base64.b64decode(envelope["ciphertext"]),
    )
    inner = json.loads(
        backend.decrypt(encrypted, PASSPHRASE, purpose=PORTABLE_MIGRATION_PURPOSE).decode("utf-8")
    )
    inner["manifest"]["files"][0]["sha256"] = "0" * 64
    rewritten = backend.encrypt(
        json.dumps(inner, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        PASSPHRASE,
        purpose=PORTABLE_MIGRATION_PURPOSE,
    )
    envelope["parameters"] = rewritten.parameters
    envelope["ciphertext"] = base64.b64encode(rewritten.ciphertext).decode("ascii")
    package_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(MigrationValidationError, match="checksum"):
        service.validate_package(package_path, PASSPHRASE)
    destination_store = _local_store(tmp_path / "destination-config.enc")
    assert not destination_store.exists


def test_default_migration_manifests_are_explicit_and_exclude_machine_state():
    assert default_migration_path_specs(MigrationScope.CONFIGURATION_ONLY) == ()
    full_paths = {item.path for item in default_migration_path_specs(MigrationScope.FULL)}
    assert full_paths == {
        "data/automation.sqlite3",
        "data/processed_platform_orders.json",
        "data/shipment_queue.sqlite3",
        "data/china_workdays.json",
        "rules",
    }
    assert ".env" not in full_paths
    assert "config.enc" not in full_paths
    assert "browser_profile" not in full_paths


@pytest.mark.parametrize(
    "dangerous_path",
    ["desktop_main.py", "erp_automation/app.py", "_internal/plugin.pyd", "rules/tool.py"],
)
def test_portable_migration_rejects_code_and_runtime_paths(tmp_path, dangerous_path):
    workspace = tmp_path / "source"
    target = workspace.joinpath(*dangerous_path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"untrusted")

    with pytest.raises(MigrationValidationError):
        _portable_service().export_package(
            ConfigurationDocument(values={}),
            tmp_path / "dangerous.erp-migration",
            PASSPHRASE,
            scope=MigrationScope.FULL,
            workspace_root=workspace,
            path_specs=(MigrationPathSpec(dangerous_path, required=True),),
        )


def test_import_rejects_authenticated_package_that_targets_program_code(tmp_path):
    service = _portable_service()
    backend = service.backend
    workspace = tmp_path / "source"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "automation.sqlite3").write_bytes(b"state")
    package_path = tmp_path / "full.erp-migration"
    service.export_package(
        ConfigurationDocument(values={}),
        package_path,
        PASSPHRASE,
        scope=MigrationScope.FULL,
        workspace_root=workspace,
        path_specs=(MigrationPathSpec("data/automation.sqlite3", required=True),),
    )

    envelope = json.loads(package_path.read_text(encoding="utf-8"))
    inner = json.loads(
        backend.decrypt(
            PortableEncryptedData(
                backend_name=backend.name,
                parameters=envelope["parameters"],
                ciphertext=base64.b64decode(envelope["ciphertext"]),
            ),
            PASSPHRASE,
            purpose=PORTABLE_MIGRATION_PURPOSE,
        ).decode("utf-8")
    )
    inner["manifest"]["files"][0]["path"] = "desktop_main.py"
    inner["files"] = {"desktop_main.py": inner["files"].pop("data/automation.sqlite3")}
    rewritten = backend.encrypt(
        json.dumps(inner, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        PASSPHRASE,
        purpose=PORTABLE_MIGRATION_PURPOSE,
    )
    envelope["parameters"] = rewritten.parameters
    envelope["ciphertext"] = base64.b64encode(rewritten.ciphertext).decode("ascii")
    package_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(MigrationValidationError):
        service.validate_package(package_path, PASSPHRASE)


def test_short_portable_password_is_rejected_without_echoing_it(tmp_path):
    short_password = "tiny"
    with pytest.raises(MigrationValidationError) as captured:
        _portable_service().export_package(
            ConfigurationDocument(values={}),
            tmp_path / "package.erp-migration",
            short_password,
        )
    assert short_password not in str(captured.value)
