"""Configuration and migration errors with deliberately non-secret messages."""

from __future__ import annotations


class ConfigurationError(RuntimeError):
    """Base class for encrypted configuration failures."""


class ConfigurationValidationError(ConfigurationError):
    """The decrypted configuration or an envelope is structurally invalid."""


class ConfigurationDependencyError(ConfigurationError):
    """An optional production cryptography dependency is unavailable."""


class ConfigurationPlatformError(ConfigurationError):
    """A platform-specific encryption backend cannot run on this platform."""


class ConfigurationDecryptionError(ConfigurationError):
    """Encrypted data could not be authenticated or decrypted."""


class MigrationValidationError(ConfigurationError):
    """A portable migration package failed validation."""


class MigrationImportError(ConfigurationError):
    """A validated migration package could not be committed locally."""
