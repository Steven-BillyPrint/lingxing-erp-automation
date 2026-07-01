from __future__ import annotations

from pathlib import Path

from .constants import FALSE_VALUES, TRUE_VALUES
from .models import LoginConfig


def read_lingxing_env(path: str | Path) -> dict[str, str]:
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

def load_login_config(env_path: str | Path) -> LoginConfig:
    values = read_lingxing_env(env_path)
    return LoginConfig(
        account=values.get("LINGXING_ACCOUNT") or None,
        password=values.get("LINGXING_PASSWORD") or None,
        remember_login=parse_env_bool(values.get("LINGXING_REMEMBER_LOGIN"), default=True),
    )
