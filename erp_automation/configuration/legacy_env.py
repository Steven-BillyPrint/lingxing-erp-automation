"""Permissive CLI environment reader retained for backward compatibility."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

ConfigurationSource = str | Path | Mapping[str, Any]


def read_legacy_env(source: ConfigurationSource) -> dict[str, Any]:
    """读取领星登录环境变量文件，并兼容注释、空行和引号格式。"""
    if isinstance(source, Mapping):
        # The desktop application passes decrypted values directly in memory.
        # A shallow copy prevents consumers from mutating the shared document;
        # this code path never serializes or logs the values.
        return dict(source)

    env_path = Path(source)
    values: dict[str, Any] = {}
    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name.strip()] = value
    return values

