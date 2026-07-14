"""Import legacy .env configuration without exposing values in diagnostics."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .errors import ConfigurationValidationError
from .models import ConfigurationDocument, EnvImportResult
from .storage import EncryptedConfigurationStore


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip_unquoted_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.strip()


def _parse_env_value(raw_value: str, *, line_number: int) -> str:
    value = _strip_unquoted_comment(raw_value.strip())
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ConfigurationValidationError(
                f"Invalid quoted value in .env at line {line_number}."
            )
        # Match the project's existing .env behavior: remove the wrapping quotes
        # but do not interpolate variables or reinterpret password backslashes.
        return value[1:-1]
    return value


def parse_env_file(path: str | Path, *, strict: bool = True) -> dict[str, str]:
    """Parse a dotenv file without interpolation and without logging values."""

    env_path = Path(path)
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            if strict:
                raise ConfigurationValidationError(
                    f"Invalid .env assignment at line {line_number}."
                )
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME_RE.fullmatch(name):
            if strict:
                raise ConfigurationValidationError(
                    f"Invalid .env variable name at line {line_number}."
                )
            continue
        values[name] = _parse_env_value(raw_value, line_number=line_number)
    return values


def import_env_file(
    store: EncryptedConfigurationStore,
    env_path: str | Path,
    *,
    include_keys: Iterable[str] | None = None,
    overwrite: bool = True,
    strict: bool = True,
) -> EnvImportResult:
    """Merge selected .env values into the encrypted local store."""

    parsed = parse_env_file(env_path, strict=strict)
    allowed = {str(key) for key in include_keys} if include_keys is not None else None
    selected = {
        key: value
        for key, value in parsed.items()
        if allowed is None or key in allowed
    }
    current: dict[str, object] = {}
    if store.exists:
        current = dict(store.load().values)
    imported: list[str] = []
    skipped = len(parsed) - len(selected)
    for key, value in selected.items():
        if key in current and not overwrite:
            skipped += 1
            continue
        current[key] = value
        imported.append(key)
    if imported or not store.exists:
        store.save(ConfigurationDocument(values=current))
    return EnvImportResult(
        imported_count=len(imported),
        skipped_count=skipped,
        imported_keys=tuple(sorted(imported)),
    )
