"""Password-protected, validated migration packages for moving between PCs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .crypto import (
    PORTABLE_MIGRATION_PURPOSE,
    Argon2idAesGcmBackend,
    PortableEncryptedData,
    PortableEncryptionBackend,
)
from .errors import (
    ConfigurationDecryptionError,
    MigrationImportError,
    MigrationValidationError,
)
from .models import (
    CONFIGURATION_SCHEMA_VERSION,
    ConfigurationDocument,
    MigrationFileEntry,
    MigrationImportResult,
    MigrationManifest,
    MigrationPathSpec,
    MigrationScope,
    canonical_json_bytes,
    normalize_relative_path,
)
from .storage import EncryptedConfigurationStore, atomic_write_bytes, backup_path_for


PORTABLE_ENVELOPE_FORMAT = "erp-automation.portable-migration"
PORTABLE_ENVELOPE_VERSION = 1
MINIMUM_PORTABLE_PASSPHRASE_LENGTH = 12
DEFAULT_MAX_PACKAGE_BYTES = 1024 * 1024 * 1024


DEFAULT_FULL_MIGRATION_PATHS: tuple[MigrationPathSpec, ...] = (
    MigrationPathSpec("data/automation.sqlite3", required=False),
    MigrationPathSpec("data/processed_platform_orders.json", required=False),
    MigrationPathSpec("data/shipment_queue.sqlite3", required=False),
    MigrationPathSpec("data/china_workdays.json", required=False),
    MigrationPathSpec("rules", required=False),
)

FULL_MIGRATION_NOTES: tuple[str, ...] = (
    "The plaintext .env file is excluded because its values are already inside the encrypted configuration.",
    "config.enc is excluded because Windows DPAPI ciphertext is not portable between PCs.",
    "browser_profile is excluded because browser cookies can be machine-bound; sign in again on the destination PC.",
    "logs, debug captures, virtual environments, and generated outputs are excluded.",
)

_PROHIBITED_TOP_LEVEL_PATHS = {
    ".git",
    ".venv",
    "browser_profile",
    "debug",
    "logs",
    "outputs",
    "__pycache__",
}
_PROHIBITED_FILE_NAMES = {".env", "config.enc", "config.enc.bak"}
_ALLOWED_EXACT_BUNDLE_PATHS = {
    "data/automation.sqlite3",
    "data/shipment_queue.sqlite3",
    "data/china_workdays.json",
    "data/processed_platform_orders.json",
}
_MAX_RULE_FILE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ValidatedMigrationPackage:
    manifest: MigrationManifest
    configuration: ConfigurationDocument
    files: dict[str, bytes]

    def __repr__(self) -> str:
        return (
            f"ValidatedMigrationPackage(scope={self.manifest.scope.value!r}, "
            f"configuration_key_count={len(self.configuration.values)}, "
            f"file_count={len(self.files)}, values=<redacted>)"
        )


def default_migration_path_specs(scope: MigrationScope | str) -> tuple[MigrationPathSpec, ...]:
    normalized_scope = _coerce_scope(scope)
    if normalized_scope is MigrationScope.CONFIGURATION_ONLY:
        return ()
    return DEFAULT_FULL_MIGRATION_PATHS


def _coerce_scope(scope: MigrationScope | str) -> MigrationScope:
    if isinstance(scope, MigrationScope):
        return scope
    try:
        return MigrationScope(str(scope))
    except ValueError as exc:
        raise MigrationValidationError("Migration scope is invalid.") from exc


def _validate_passphrase(passphrase: str) -> None:
    if not isinstance(passphrase, str) or len(passphrase) < MINIMUM_PORTABLE_PASSPHRASE_LENGTH:
        raise MigrationValidationError(
            f"Portable migration password must contain at least {MINIMUM_PORTABLE_PASSPHRASE_LENGTH} characters."
        )


def _validate_allowed_bundle_path(path: str) -> None:
    normalized = normalize_relative_path(path)
    parts = PurePosixPath(normalized).parts
    if parts[0].lower() in _PROHIBITED_TOP_LEVEL_PATHS:
        raise MigrationValidationError(
            f"The path '{parts[0]}' is intentionally excluded from portable migration."
        )
    if parts[-1].lower() in _PROHIBITED_FILE_NAMES:
        raise MigrationValidationError(
            "Raw .env and machine-local config.enc files may not be included in a portable package."
        )
    if normalized in _ALLOWED_EXACT_BUNDLE_PATHS or normalized == "rules":
        return
    if (
        parts[0].casefold() == "rules"
        and len(parts) >= 2
        and parts[-1].casefold().endswith(".json")
        and not any(part.startswith(".") for part in parts[1:])
    ):
        return
    raise MigrationValidationError(
        "Portable migration files are restricted to the two state databases, "
        "the workday/legacy JSON files, and JSON files below rules/."
    )


def _safe_source_path(root: Path, relative_path: str) -> Path:
    normalized = normalize_relative_path(relative_path)
    _validate_allowed_bundle_path(normalized)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    root_resolved = root.resolve()
    try:
        candidate.resolve(strict=False).relative_to(root_resolved)
    except ValueError as exc:
        raise MigrationValidationError("A migration source path escapes the workspace root.") from exc
    current = candidate
    while current != root and current != current.parent:
        if current.exists() and current.is_symlink():
            raise MigrationValidationError("Symbolic links are not allowed in migration packages.")
        current = current.parent
    return candidate


def _safe_destination_path(root: Path, relative_path: str) -> Path:
    normalized = normalize_relative_path(relative_path)
    _validate_allowed_bundle_path(normalized)
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*PurePosixPath(normalized).parts)
    try:
        candidate.resolve(strict=False).relative_to(root_resolved)
    except ValueError as exc:
        raise MigrationValidationError("A migration destination path escapes the destination root.") from exc
    current = candidate.parent
    while current != root_resolved and current != current.parent:
        if current.exists() and current.is_symlink():
            raise MigrationValidationError("Migration destination paths may not traverse symbolic links.")
        current = current.parent
    return candidate


def _expand_path_spec(root: Path, spec: MigrationPathSpec) -> list[tuple[str, Path, bool]]:
    normalized = spec.normalized_path()
    source = _safe_source_path(root, normalized)
    if not source.exists():
        if spec.required:
            raise MigrationValidationError(f"Required migration path is missing: {normalized}")
        return []
    if source.is_symlink():
        raise MigrationValidationError("Symbolic links are not allowed in migration packages.")
    if source.is_file():
        return [(normalized, source, spec.required)]
    if not source.is_dir():
        raise MigrationValidationError(f"Migration path is not a regular file or directory: {normalized}")
    output: list[tuple[str, Path, bool]] = []
    for child in sorted(source.rglob("*"), key=lambda item: item.as_posix().lower()):
        if child.is_symlink():
            raise MigrationValidationError("Symbolic links are not allowed in migration packages.")
        if not child.is_file():
            continue
        relative = child.relative_to(root).as_posix()
        _validate_allowed_bundle_path(relative)
        output.append((relative, child, spec.required))
    return output


def _collect_workspace_files(
    root: Path,
    specs: Iterable[MigrationPathSpec],
    *,
    max_total_bytes: int,
) -> dict[str, tuple[bytes, bool]]:
    collected: dict[str, tuple[bytes, bool]] = {}
    total_size = 0
    for spec in specs:
        for relative, source, required in _expand_path_spec(root, spec):
            if relative in collected:
                raise MigrationValidationError(f"Duplicate migration path: {relative}")
            data = source.read_bytes()
            total_size += len(data)
            if total_size > max_total_bytes:
                raise MigrationValidationError("Migration workspace files exceed the configured size limit.")
            collected[relative] = (data, required)
    return collected


class PortableMigrationService:
    """Create, validate, and import authenticated cross-computer packages."""

    def __init__(
        self,
        *,
        backend: PortableEncryptionBackend | None = None,
        max_total_file_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
    ) -> None:
        self.backend = backend or Argon2idAesGcmBackend()
        self.max_total_file_bytes = max_total_file_bytes

    def export_package(
        self,
        document: ConfigurationDocument,
        destination: str | Path,
        passphrase: str,
        *,
        scope: MigrationScope | str = MigrationScope.CONFIGURATION_ONLY,
        workspace_root: str | Path | None = None,
        path_specs: Iterable[MigrationPathSpec] | None = None,
    ) -> MigrationManifest:
        _validate_passphrase(passphrase)
        document.validate()
        normalized_scope = _coerce_scope(scope)
        supplied_specs = tuple(path_specs) if path_specs is not None else None
        specs = supplied_specs if supplied_specs is not None else default_migration_path_specs(normalized_scope)
        if normalized_scope is MigrationScope.CONFIGURATION_ONLY and specs:
            raise MigrationValidationError("Configuration-only migrations may not include workspace paths.")
        files: dict[str, tuple[bytes, bool]] = {}
        if normalized_scope is MigrationScope.FULL:
            if workspace_root is None:
                raise MigrationValidationError("Full migration export requires a workspace_root.")
            files = _collect_workspace_files(
                Path(workspace_root),
                specs,
                max_total_bytes=self.max_total_file_bytes,
            )
        entries = tuple(
            MigrationFileEntry.from_bytes(path, data, required=required)
            for path, (data, required) in sorted(files.items())
        )
        manifest = MigrationManifest(
            scope=normalized_scope,
            configuration_schema_version=document.schema_version,
            files=entries,
            notes=FULL_MIGRATION_NOTES if normalized_scope is MigrationScope.FULL else (),
        )
        inner_payload = {
            "manifest": manifest.to_payload(),
            "configuration": document.to_payload(),
            "files": {
                path: base64.b64encode(data).decode("ascii")
                for path, (data, _required) in sorted(files.items())
            },
        }
        encrypted = self.backend.encrypt(
            canonical_json_bytes(inner_payload),
            passphrase,
            purpose=PORTABLE_MIGRATION_PURPOSE,
        )
        if encrypted.backend_name != self.backend.name:
            raise MigrationValidationError("Portable backend returned an inconsistent identifier.")
        envelope = {
            "format": PORTABLE_ENVELOPE_FORMAT,
            "format_version": PORTABLE_ENVELOPE_VERSION,
            "backend": encrypted.backend_name,
            "parameters": encrypted.parameters,
            "ciphertext": base64.b64encode(encrypted.ciphertext).decode("ascii"),
        }
        target = Path(destination)
        atomic_write_bytes(target, canonical_json_bytes(envelope), backup_path=backup_path_for(target))
        return manifest

    def export_from_store(
        self,
        store: EncryptedConfigurationStore,
        destination: str | Path,
        passphrase: str,
        **kwargs: Any,
    ) -> MigrationManifest:
        return self.export_package(store.load(), destination, passphrase, **kwargs)

    def validate_package(
        self,
        package_path: str | Path,
        passphrase: str,
    ) -> ValidatedMigrationPackage:
        _validate_passphrase(passphrase)
        try:
            envelope = json.loads(Path(package_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationValidationError("Portable migration envelope is unreadable or invalid.") from exc
        if not isinstance(envelope, dict):
            raise MigrationValidationError("Portable migration envelope must be a JSON object.")
        if envelope.get("format") != PORTABLE_ENVELOPE_FORMAT:
            raise MigrationValidationError("Portable migration format identifier is invalid.")
        if envelope.get("format_version") != PORTABLE_ENVELOPE_VERSION:
            raise MigrationValidationError("Portable migration format version is unsupported.")
        if envelope.get("backend") != self.backend.name:
            raise MigrationValidationError("Portable migration encryption backend does not match.")
        parameters = envelope.get("parameters")
        if not isinstance(parameters, dict):
            raise MigrationValidationError("Portable migration encryption parameters are invalid.")
        encoded_ciphertext = envelope.get("ciphertext")
        if not isinstance(encoded_ciphertext, str):
            raise MigrationValidationError("Portable migration ciphertext is invalid.")
        try:
            ciphertext = base64.b64decode(encoded_ciphertext, validate=True)
        except Exception as exc:
            raise MigrationValidationError("Portable migration ciphertext is invalid.") from exc
        try:
            plaintext = self.backend.decrypt(
                PortableEncryptedData(
                    backend_name=self.backend.name,
                    parameters=parameters,
                    ciphertext=ciphertext,
                ),
                passphrase,
                purpose=PORTABLE_MIGRATION_PURPOSE,
            )
        except ConfigurationDecryptionError:
            raise
        except Exception as exc:
            raise ConfigurationDecryptionError(
                "Portable migration authentication failed. The password may be wrong or the package may be damaged."
            ) from exc
        try:
            inner = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationValidationError("Decrypted portable migration payload is invalid.") from exc
        return self._validate_inner_payload(inner)

    def _validate_inner_payload(self, inner: Any) -> ValidatedMigrationPackage:
        if not isinstance(inner, dict):
            raise MigrationValidationError("Decrypted portable migration payload must be an object.")
        manifest = MigrationManifest.from_payload(inner.get("manifest"))
        configuration = ConfigurationDocument.from_payload(inner.get("configuration"))
        if manifest.configuration_schema_version != configuration.schema_version:
            raise MigrationValidationError("Manifest and configuration schema versions do not match.")
        encoded_files = inner.get("files")
        if not isinstance(encoded_files, dict):
            raise MigrationValidationError("Portable migration files payload must be an object.")
        manifest_paths = {entry.path for entry in manifest.files}
        if set(encoded_files) != manifest_paths:
            raise MigrationValidationError("Portable migration manifest and file payload paths do not match.")
        files: dict[str, bytes] = {}
        total_size = 0
        entries_by_path = {entry.path: entry for entry in manifest.files}
        for path, encoded in encoded_files.items():
            normalized = normalize_relative_path(path)
            _validate_allowed_bundle_path(normalized)
            if normalized != path or not isinstance(encoded, str):
                raise MigrationValidationError("Portable migration contains an invalid file payload.")
            try:
                data = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise MigrationValidationError("Portable migration contains an invalid file payload.") from exc
            total_size += len(data)
            if total_size > self.max_total_file_bytes:
                raise MigrationValidationError("Portable migration files exceed the configured size limit.")
            if normalized.startswith("rules/") and len(data) > _MAX_RULE_FILE_BYTES:
                raise MigrationValidationError("A portable migration rule file exceeds the size limit.")
            entry = entries_by_path[path]
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise MigrationValidationError("Portable migration file checksum validation failed.")
            files[path] = data
        if manifest.scope is MigrationScope.CONFIGURATION_ONLY and files:
            raise MigrationValidationError("Configuration-only migration unexpectedly contains files.")
        return ValidatedMigrationPackage(
            manifest=manifest,
            configuration=configuration,
            files=files,
        )

    def import_package(
        self,
        package_path: str | Path,
        passphrase: str,
        *,
        config_store: EncryptedConfigurationStore,
        destination_root: str | Path | None = None,
        overwrite: bool = False,
    ) -> MigrationImportResult:
        validated = self.validate_package(package_path, passphrase)
        if config_store.exists and not overwrite:
            raise MigrationImportError(
                "A local encrypted configuration already exists; pass overwrite=True to replace it."
            )
        root: Path | None = None
        destination_files: list[tuple[str, Path, bytes]] = []
        if validated.manifest.scope is MigrationScope.FULL:
            if destination_root is None:
                raise MigrationImportError("Full migration import requires a destination_root.")
            root = Path(destination_root)
            root.mkdir(parents=True, exist_ok=True)
            for relative, data in sorted(validated.files.items()):
                destination = _safe_destination_path(root, relative)
                if destination.exists() and not overwrite:
                    raise MigrationImportError(
                        f"Migration destination already exists: {relative}. Pass overwrite=True to replace it."
                    )
                if destination.exists() and not destination.is_file():
                    raise MigrationImportError(f"Migration destination is not a regular file: {relative}")
                destination_files.append((relative, destination, data))

        # A migration may move credentials and state, but it must never carry
        # an already-lifted production write switch onto an unfamiliar PC.
        # Keep generic configuration documents unchanged; production desktop
        # documents always contain this key through their normalized defaults.
        destination_configuration = validated.configuration
        if "safety.erp_writes_enabled" in validated.configuration.values:
            safe_values = dict(validated.configuration.values)
            safe_values["safety.erp_writes_enabled"] = False
            destination_configuration = ConfigurationDocument(
                values=safe_values,
                schema_version=validated.configuration.schema_version,
                updated_at=validated.configuration.updated_at,
            )

        # Encrypt the destination's DPAPI/local envelope before changing any files.
        encoded_configuration = config_store.encode(destination_configuration)
        writes: list[tuple[Path, bytes, Path]] = [
            (config_store.path, encoded_configuration, config_store.backup_path)
        ]
        writes.extend(
            (destination, data, backup_path_for(destination))
            for _relative, destination, data in destination_files
        )
        self._commit_validated_writes(writes)
        imported_paths = tuple(relative for relative, _destination, _data in destination_files)
        return MigrationImportResult(
            scope=validated.manifest.scope,
            imported_file_count=len(imported_paths),
            imported_paths=imported_paths,
            configuration_key_count=len(destination_configuration.values),
        )

    @staticmethod
    def _commit_validated_writes(writes: list[tuple[Path, bytes, Path]]) -> None:
        """Commit validated writes atomically per file and roll back process errors."""

        old_values: dict[Path, bytes | None] = {}
        committed: list[Path] = []
        try:
            for target, _data, backup in writes:
                old = target.read_bytes() if target.is_file() else None
                old_values[target] = old
                if old is not None:
                    atomic_write_bytes(backup, old, backup_path=None)
            for target, data, _backup in writes:
                atomic_write_bytes(target, data, backup_path=None)
                committed.append(target)
        except Exception as exc:
            for target in reversed(committed):
                old = old_values.get(target)
                try:
                    if old is None:
                        target.unlink(missing_ok=True)
                    else:
                        atomic_write_bytes(target, old, backup_path=None)
                except OSError:
                    pass
            raise MigrationImportError(
                "Migration import could not be committed; completed writes were rolled back where possible."
            ) from exc
