"""Versioned, redaction-safe models for encrypted configuration and migration."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .errors import ConfigurationValidationError, MigrationValidationError


CONFIGURATION_SCHEMA = "erp-automation.configuration"
CONFIGURATION_SCHEMA_VERSION = 1
MIGRATION_MANIFEST_SCHEMA = "erp-automation.migration-manifest"
MIGRATION_MANIFEST_VERSION = 1


def utc_now_text() -> str:
    """Return a stable UTC timestamp suitable for persisted metadata."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically for encryption and hashing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_json_value(value: Any, *, location: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigurationValidationError(f"{location} contains a non-finite number.")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, location=f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ConfigurationValidationError(f"{location} contains an invalid object key.")
            _validate_json_value(item, location=f"{location}.{key}")
        return
    raise ConfigurationValidationError(f"{location} contains a value that is not JSON-compatible.")


@dataclass
class ConfigurationDocument:
    """The plaintext document that is encrypted at rest.

    ``repr`` intentionally reports only key names and counts. Callers can access
    ``values`` explicitly, but normal diagnostics will not print credentials.
    """

    values: dict[str, Any]
    schema_version: int = CONFIGURATION_SCHEMA_VERSION
    updated_at: str = field(default_factory=utc_now_text)

    def validate(self) -> None:
        if self.schema_version != CONFIGURATION_SCHEMA_VERSION:
            raise ConfigurationValidationError(
                f"Unsupported configuration schema version: {self.schema_version}."
            )
        if not isinstance(self.values, dict):
            raise ConfigurationValidationError("Configuration values must be a JSON object.")
        for key, value in self.values.items():
            if not isinstance(key, str) or not key.strip():
                raise ConfigurationValidationError("Configuration keys must be non-empty strings.")
            _validate_json_value(value, location=f"configuration.{key}")
        if not isinstance(self.updated_at, str) or not self.updated_at:
            raise ConfigurationValidationError("Configuration updated_at must be a non-empty string.")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": CONFIGURATION_SCHEMA,
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "values": self.values,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "ConfigurationDocument":
        if not isinstance(payload, dict):
            raise ConfigurationValidationError("Configuration payload must be a JSON object.")
        if payload.get("schema") != CONFIGURATION_SCHEMA:
            raise ConfigurationValidationError("Configuration schema identifier is invalid.")
        version = payload.get("schema_version")
        if not isinstance(version, int):
            raise ConfigurationValidationError("Configuration schema_version must be an integer.")
        values = payload.get("values")
        if not isinstance(values, dict):
            raise ConfigurationValidationError("Configuration values must be a JSON object.")
        document = cls(
            values=values,
            schema_version=version,
            updated_at=str(payload.get("updated_at") or ""),
        )
        document.validate()
        return document

    def __repr__(self) -> str:
        keys = sorted(str(key) for key in self.values)
        return (
            f"ConfigurationDocument(schema_version={self.schema_version}, "
            f"value_count={len(keys)}, keys={keys!r}, values=<redacted>)"
        )


class MigrationScope(str, Enum):
    CONFIGURATION_ONLY = "configuration_only"
    FULL = "full"


def normalize_relative_path(value: str) -> str:
    """Normalize and validate a migration path without touching the filesystem."""

    raw = str(value or "").replace("\\", "/").strip()
    if not raw or "\x00" in raw:
        raise MigrationValidationError("Migration paths must be non-empty relative paths.")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise MigrationValidationError("Absolute paths are not allowed in migration packages.")
    if any(part in {"", ".", ".."} for part in posix.parts):
        raise MigrationValidationError("Migration paths may not contain '.' or '..' segments.")
    return posix.as_posix()


@dataclass(frozen=True)
class MigrationPathSpec:
    path: str
    required: bool = False

    def normalized_path(self) -> str:
        return normalize_relative_path(self.path)


@dataclass(frozen=True)
class MigrationFileEntry:
    path: str
    size: int
    sha256: str
    required: bool = False

    def validate(self) -> None:
        normalized = normalize_relative_path(self.path)
        if normalized != self.path:
            raise MigrationValidationError("Migration manifest paths must already be normalized.")
        if not isinstance(self.size, int) or self.size < 0:
            raise MigrationValidationError("Migration manifest contains an invalid file size.")
        if not isinstance(self.sha256, str) or len(self.sha256) != 64:
            raise MigrationValidationError("Migration manifest contains an invalid SHA-256 digest.")
        try:
            int(self.sha256, 16)
        except ValueError as exc:
            raise MigrationValidationError("Migration manifest contains an invalid SHA-256 digest.") from exc

    @classmethod
    def from_bytes(cls, path: str, data: bytes, *, required: bool = False) -> "MigrationFileEntry":
        return cls(
            path=normalize_relative_path(path),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            required=required,
        )

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "required": self.required,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "MigrationFileEntry":
        if not isinstance(payload, dict):
            raise MigrationValidationError("Migration file entries must be JSON objects.")
        entry = cls(
            path=str(payload.get("path") or ""),
            size=payload.get("size"),
            sha256=str(payload.get("sha256") or ""),
            required=bool(payload.get("required", False)),
        )
        entry.validate()
        return entry


@dataclass(frozen=True)
class MigrationManifest:
    scope: MigrationScope
    configuration_schema_version: int
    files: tuple[MigrationFileEntry, ...] = ()
    created_at: str = field(default_factory=utc_now_text)
    notes: tuple[str, ...] = ()
    manifest_version: int = MIGRATION_MANIFEST_VERSION

    def validate(self) -> None:
        if self.manifest_version != MIGRATION_MANIFEST_VERSION:
            raise MigrationValidationError(
                f"Unsupported migration manifest version: {self.manifest_version}."
            )
        if self.configuration_schema_version != CONFIGURATION_SCHEMA_VERSION:
            raise MigrationValidationError("Migration configuration schema version is unsupported.")
        if not isinstance(self.scope, MigrationScope):
            raise MigrationValidationError("Migration scope is invalid.")
        if self.scope is MigrationScope.CONFIGURATION_ONLY and self.files:
            raise MigrationValidationError("Configuration-only migrations may not contain workspace files.")
        seen: set[str] = set()
        for entry in self.files:
            entry.validate()
            if entry.path in seen:
                raise MigrationValidationError("Migration manifest contains a duplicate file path.")
            seen.add(entry.path)
        if not isinstance(self.created_at, str) or not self.created_at:
            raise MigrationValidationError("Migration manifest created_at is invalid.")
        if any(not isinstance(note, str) for note in self.notes):
            raise MigrationValidationError("Migration manifest notes must be strings.")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": MIGRATION_MANIFEST_SCHEMA,
            "manifest_version": self.manifest_version,
            "scope": self.scope.value,
            "configuration_schema_version": self.configuration_schema_version,
            "created_at": self.created_at,
            "files": [entry.to_payload() for entry in self.files],
            "notes": list(self.notes),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "MigrationManifest":
        if not isinstance(payload, dict):
            raise MigrationValidationError("Migration manifest must be a JSON object.")
        if payload.get("schema") != MIGRATION_MANIFEST_SCHEMA:
            raise MigrationValidationError("Migration manifest schema identifier is invalid.")
        try:
            scope = MigrationScope(str(payload.get("scope") or ""))
        except ValueError as exc:
            raise MigrationValidationError("Migration scope is invalid.") from exc
        files_payload = payload.get("files")
        if not isinstance(files_payload, list):
            raise MigrationValidationError("Migration manifest files must be a list.")
        notes_payload = payload.get("notes", [])
        if not isinstance(notes_payload, list):
            raise MigrationValidationError("Migration manifest notes must be a list.")
        manifest = cls(
            scope=scope,
            configuration_schema_version=payload.get("configuration_schema_version"),
            files=tuple(MigrationFileEntry.from_payload(item) for item in files_payload),
            created_at=str(payload.get("created_at") or ""),
            notes=tuple(notes_payload),
            manifest_version=payload.get("manifest_version"),
        )
        manifest.validate()
        return manifest


@dataclass(frozen=True)
class EnvImportResult:
    imported_count: int
    skipped_count: int
    imported_keys: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"EnvImportResult(imported_count={self.imported_count}, "
            f"skipped_count={self.skipped_count}, imported_keys={list(self.imported_keys)!r}, "
            "values=<redacted>)"
        )


@dataclass(frozen=True)
class MigrationImportResult:
    scope: MigrationScope
    imported_file_count: int
    imported_paths: tuple[str, ...]
    configuration_key_count: int

    def __repr__(self) -> str:
        return (
            f"MigrationImportResult(scope={self.scope.value!r}, "
            f"imported_file_count={self.imported_file_count}, "
            f"imported_paths={list(self.imported_paths)!r}, "
            f"configuration_key_count={self.configuration_key_count}, values=<redacted>)"
        )
