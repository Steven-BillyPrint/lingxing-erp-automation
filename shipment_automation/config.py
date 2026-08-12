from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SHIPMENT_QUEUE_PATH = "data/shipment_queue.sqlite3"

# Default Lingxing custom-order tag used by the shipment candidate scanner.
# The desktop application exposes this value in Settings and passes the saved
# value to every scan; the constant remains the CLI/default compatibility value.
SHIPMENT_TAG_NAME = "标发"

TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}

ConfigurationSource = str | Path | Mapping[str, Any]


@dataclass
class AlibabaLoginConfig:
    account: str | None = None
    password: str | None = None
    auto_login: bool = True

    @property
    def has_credentials(self) -> bool:
        return bool(self.account and self.password)


def read_env_file(source: ConfigurationSource) -> dict[str, Any]:
    if isinstance(source, Mapping):
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


def get_configuration_value(values: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        if key not in values:
            continue
        value = values[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return value
    return None


def configuration_source_from_args(args: Any, default_env_path: str | Path = ".env") -> ConfigurationSource:
    configuration_values = getattr(args, "configuration_values", None)
    if configuration_values is not None:
        if not isinstance(configuration_values, Mapping):
            raise TypeError("configuration_values must be a mapping")
        return configuration_values
    return getattr(args, "env_path", default_env_path)


def parse_env_bool(value: Any | None, default: bool = True) -> bool:
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


def load_alibaba_login_config(source: ConfigurationSource) -> AlibabaLoginConfig:
    values = read_env_file(source)
    return AlibabaLoginConfig(
        account=get_configuration_value(values, "alibaba.account", "ALIBABA_ACCOUNT"),
        password=get_configuration_value(values, "alibaba.password", "ALIBABA_PASSWORD"),
        auto_login=parse_env_bool(
            get_configuration_value(values, "alibaba.auto_login", "ALIBABA_AUTO_LOGIN"),
            default=True,
        ),
    )


def load_alibaba_logistics_query_login_config(
    source: ConfigurationSource,
) -> AlibabaLoginConfig:
    """Load only the dedicated read-only logistics-query credentials.

    The ordering account is deliberately not a fallback.  Logistics lookup
    must fail closed when its own credentials have not been configured.
    """

    values = read_env_file(source)
    return AlibabaLoginConfig(
        account=get_configuration_value(
            values,
            "alibaba.logistics_query.account",
            "ALIBABA_LOGISTICS_QUERY_ACCOUNT",
        ),
        password=get_configuration_value(
            values,
            "alibaba.logistics_query.password",
            "ALIBABA_LOGISTICS_QUERY_PASSWORD",
        ),
        auto_login=parse_env_bool(
            get_configuration_value(
                values,
                "alibaba.logistics_query.auto_login",
                "ALIBABA_LOGISTICS_QUERY_AUTO_LOGIN",
            ),
            default=True,
        ),
    )
