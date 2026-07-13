from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_SHIPMENT_QUEUE_PATH = "data/shipment_queue.sqlite3"

# Fill this with the dedicated ERP tag for tent auto shipment marking.
SHIPMENT_TAG_NAME = "帐篷标发"

TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}


@dataclass
class AlibabaLoginConfig:
    account: str | None = None
    password: str | None = None
    auto_login: bool = True

    @property
    def has_credentials(self) -> bool:
        return bool(self.account and self.password)


def read_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    values: dict[str, str] = {}
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


def parse_env_bool(value: str | None, default: bool = True) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return default


def load_alibaba_login_config(env_path: str | Path) -> AlibabaLoginConfig:
    values = read_env_file(env_path)
    return AlibabaLoginConfig(
        account=values.get("ALIBABA_ACCOUNT") or None,
        password=values.get("ALIBABA_PASSWORD") or None,
        auto_login=parse_env_bool(values.get("ALIBABA_AUTO_LOGIN"), default=True),
    )
