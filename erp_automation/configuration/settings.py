"""Canonical keys for the single encrypted application configuration file."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEFAULT_CONFIGURATION_VALUES: dict[str, Any] = {
    "lingxing.app_id": "",
    "lingxing.app_secret": "",
    "lingxing.api_base_url": "https://openapi.lingxing.com",
    "lingxing.account": "",
    "lingxing.password": "",
    "lingxing.remember_login": True,
    "lingxing.erp_mark.routes": {},
    "lingxing.erp_mark.outbound_strategy": "staged",
    "lingxing.erp_mark.wms_poll_attempts": 5,
    "lingxing.erp_mark.wms_poll_interval_seconds": 1,
    "lingxing.erp_mark.fast_result_attempts": 10,
    "lingxing.erp_mark.fast_result_interval_seconds": 1,
    "lingxing.write_readback_delays_seconds": [
        0, 1, 2, 5, 10, 20, 30, 45, 60, 60, 60
    ],
    "alibaba.account": "",
    "alibaba.password": "",
    "alibaba.auto_login": True,
    "amazon.lwa_client_id": "",
    "amazon.lwa_client_secret": "",
    "amazon.refresh_token": "",
    "amazon.sp_api_sandbox": False,
    "alimail.application_name": "",
    "alimail.app_id": "",
    "alimail.app_secret": "",
    "alimail.amazon_sender_email": "acs@billyprint.com",
    "alimail.independent_sender_email": "cs@billyprint.com",
    "alimail.sender_display_name": "BillyPrint Customer Service",
    "clicksend.username": "",
    "clicksend.api_key": "",
    "clicksend.sender_id": "",
    "notifications.amazon_platform_codes": ["10001"],
    "notifications.amazon_platform_names": ["amazon", "亚马逊"],
    "notifications.virtual_email_domains": {
        "amazon": ["marketplace.amazon.com"],
        "10001": ["marketplace.amazon.com"],
    },
    "paths.folder_root": r"Z:\Amazon每日订单汇总",
    "paths.custom_state_db": "data/automation.sqlite3",
    "paths.shipment_queue_db": "data/shipment_queue.sqlite3",
    "paths.browser_profile": "browser_profile",
    "paths.log_dir": "logs",
    "api.timeout_seconds": 30,
    "automation.payment_window_hours": 96,
    "logs.retention_days": 90,
    "automation.browser_fallback_enabled": True,
    "safety.erp_writes_enabled": False,
    "logs.redact_sensitive": True,
    "email.mode": "disabled",
    "capabilities.email_preview": "disabled",
}


ENV_KEY_MAP: dict[str, str] = {
    "LINGXING_APP_ID": "lingxing.app_id",
    "LINGXING_APP_SECRET": "lingxing.app_secret",
    "LINGXING_API_BASE_URL": "lingxing.api_base_url",
    "LINGXING_ACCOUNT": "lingxing.account",
    "LINGXING_PASSWORD": "lingxing.password",
    "LINGXING_REMEMBER_LOGIN": "lingxing.remember_login",
    "ALIBABA_ACCOUNT": "alibaba.account",
    "ALIBABA_PASSWORD": "alibaba.password",
    "ALIBABA_AUTO_LOGIN": "alibaba.auto_login",
    "AMAZON_LWA_CLIENT_ID": "amazon.lwa_client_id",
    "AMAZON_LWA_CLIENT_SECRET": "amazon.lwa_client_secret",
    "AMAZON_REFRESH_TOKEN": "amazon.refresh_token",
    "AMAZON_SP_API_SANDBOX": "amazon.sp_api_sandbox",
    "ALIMAIL_APPLICATION_NAME": "alimail.application_name",
    "ALIMAIL_APP_ID": "alimail.app_id",
    "ALIMAIL_APP_SECRET": "alimail.app_secret",
    "CLICKSEND_USERNAME": "clicksend.username",
    "CLICKSEND_API_KEY": "clicksend.api_key",
    "AMAZON_SP_API_ENDPOINT": "amazon.sp_api_endpoint",
}


SENSITIVE_CONFIGURATION_KEYS = frozenset(
    {
        "lingxing.app_secret",
        "lingxing.password",
        "alibaba.password",
        "amazon.lwa_client_secret",
        "amazon.refresh_token",
        "alimail.app_secret",
        "clicksend.username",
        "clicksend.api_key",
    }
)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "yes", "y", "on", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否"}:
        return False
    return default


def _as_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def with_configuration_defaults(values: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a normalized copy while enforcing non-negotiable safety policy."""

    merged = dict(DEFAULT_CONFIGURATION_VALUES)
    merged.update(dict(values or {}))
    merged["lingxing.remember_login"] = _as_bool(merged.get("lingxing.remember_login"), True)
    merged["alibaba.auto_login"] = _as_bool(merged.get("alibaba.auto_login"), True)
    merged["amazon.sp_api_sandbox"] = _as_bool(merged.get("amazon.sp_api_sandbox"), False)
    merged["automation.browser_fallback_enabled"] = _as_bool(
        merged.get("automation.browser_fallback_enabled"), True
    )
    merged["logs.redact_sensitive"] = True
    merged["safety.erp_writes_enabled"] = _as_bool(
        merged.get("safety.erp_writes_enabled"), False
    )
    merged["api.timeout_seconds"] = _as_positive_int(merged.get("api.timeout_seconds"), 30)
    # This is a confirmed business rule, not an operator tuning knob.  Always
    # normalize old 24-hour or otherwise edited configuration values to 96 so
    # the API query cannot omit orders that the business layer expects.
    merged["automation.payment_window_hours"] = 96
    merged["lingxing.api_base_url"] = "https://openapi.lingxing.com"
    merged["paths.custom_state_db"] = "data/automation.sqlite3"
    merged["paths.shipment_queue_db"] = "data/shipment_queue.sqlite3"
    merged["paths.log_dir"] = "logs"
    # These are deliberate product policies, not user-overridable defaults.
    merged["logs.retention_days"] = 90
    merged["email.mode"] = "disabled"
    # Customer e-mail delivery has not been connected yet.  Keep the dormant
    # preview implementation in source, but make the installed product fail
    # closed until a future release deliberately enables the full mail flow.
    merged["capabilities.email_preview"] = "disabled"
    return merged


def import_environment_values(values: Mapping[str, str]) -> dict[str, Any]:
    """Translate legacy .env names without retaining unknown environment data."""

    translated: dict[str, Any] = {}
    for env_key, config_key in ENV_KEY_MAP.items():
        if env_key in values:
            translated[config_key] = values[env_key]
    return with_configuration_defaults(translated)


def redacted_configuration(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return diagnostics safe to display or write to logs."""

    return {
        key: ("<已配置>" if key in SENSITIVE_CONFIGURATION_KEYS and value else value)
        for key, value in values.items()
    }
