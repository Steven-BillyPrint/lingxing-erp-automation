from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from erp_automation.configuration.legacy_env import read_legacy_env

from .constants import FALSE_VALUES, TRUE_VALUES
from .models import LoginConfig


ConfigurationSource = str | Path | Mapping[str, Any]


def read_lingxing_env(source: ConfigurationSource) -> dict[str, Any]:
    return read_legacy_env(source)


def get_configuration_value(values: Mapping[str, Any], *keys: str) -> Any | None:
    """Return the first configured alias without exposing it in diagnostics."""

    for key in keys:
        if key not in values:
            continue
        value = values[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return value
    return None


def configuration_source_from_args(args: Any, default_env_path: str | Path = ".env") -> ConfigurationSource:
    """Prefer an in-memory desktop configuration over the legacy dotenv path."""

    configuration_values = getattr(args, "configuration_values", None)
    if configuration_values is not None:
        if not isinstance(configuration_values, Mapping):
            raise TypeError("configuration_values must be a mapping")
        return configuration_values
    return getattr(args, "env_path", default_env_path)


def parse_env_bool(value: Any | None, default: bool = True) -> bool:
    """将环境变量中的布尔文本解析为程序可用的布尔值。"""
    if isinstance(value, bool):
        return value
    if value is None or not str(value).strip():
        return default
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return default


def load_login_config(source: ConfigurationSource) -> LoginConfig:
    """加载领星登录配置，供浏览器自动登录流程使用。"""
    values = read_lingxing_env(source)
    return LoginConfig(
        account=get_configuration_value(values, "lingxing.account", "LINGXING_ACCOUNT"),
        password=get_configuration_value(values, "lingxing.password", "LINGXING_PASSWORD"),
        remember_login=parse_env_bool(
            get_configuration_value(
                values,
                "lingxing.remember_login",
                "LINGXING_REMEMBER_LOGIN",
            ),
            default=True,
        ),
    )
