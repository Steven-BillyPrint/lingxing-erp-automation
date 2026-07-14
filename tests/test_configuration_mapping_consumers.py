from __future__ import annotations

from types import MappingProxyType, SimpleNamespace

import pytest

from lingxing_automation.config import (
    configuration_source_from_args as lingxing_source_from_args,
    load_login_config,
    read_lingxing_env,
)
from lingxing_automation.services.amazon_order_quantity import (
    DEFAULT_SP_API_ENDPOINT,
    SANDBOX_SP_API_ENDPOINT,
    AmazonOrderQuantityClient,
    AmazonOrderQuantityConfig,
)
from shipment_automation.config import (
    configuration_source_from_args as shipment_source_from_args,
    load_alibaba_login_config,
)


def test_lingxing_login_accepts_canonical_in_memory_configuration() -> None:
    values = MappingProxyType(
        {
            "lingxing.account": "desktop@example.com",
            "lingxing.password": "in-memory-secret",
            "lingxing.remember_login": False,
        }
    )

    config = load_login_config(values)

    assert config.account == "desktop@example.com"
    assert config.password == "in-memory-secret"
    assert config.remember_login is False
    assert read_lingxing_env(values) is not values


def test_lingxing_login_accepts_legacy_keys_and_prefers_nonempty_canonical_keys() -> None:
    config = load_login_config(
        {
            "lingxing.account": "canonical@example.com",
            "lingxing.password": "",
            "LINGXING_ACCOUNT": "legacy@example.com",
            "LINGXING_PASSWORD": "legacy-secret",
            "LINGXING_REMEMBER_LOGIN": "false",
        }
    )

    assert config.account == "canonical@example.com"
    assert config.password == "legacy-secret"
    assert config.remember_login is False


def test_alibaba_login_accepts_canonical_and_legacy_in_memory_keys() -> None:
    canonical = load_alibaba_login_config(
        {
            "alibaba.account": "buyer@example.com",
            "alibaba.password": "canonical-secret",
            "alibaba.auto_login": False,
        }
    )
    legacy = load_alibaba_login_config(
        {
            "ALIBABA_ACCOUNT": "legacy@example.com",
            "ALIBABA_PASSWORD": "legacy-secret",
            "ALIBABA_AUTO_LOGIN": "true",
        }
    )

    assert canonical.account == "buyer@example.com"
    assert canonical.password == "canonical-secret"
    assert canonical.auto_login is False
    assert legacy.account == "legacy@example.com"
    assert legacy.password == "legacy-secret"
    assert legacy.auto_login is True


def test_amazon_quantity_config_accepts_canonical_in_memory_configuration() -> None:
    config = AmazonOrderQuantityConfig.from_env(
        {
            "amazon.refresh_token": "refresh",
            "amazon.lwa_client_id": "client",
            "amazon.lwa_client_secret": "secret",
            "amazon.sp_api_sandbox": True,
        }
    )

    assert config is not None
    assert config.refresh_token == "refresh"
    assert config.client_id == "client"
    assert config.client_secret == "secret"
    assert config.endpoint == SANDBOX_SP_API_ENDPOINT


def test_amazon_quantity_config_keeps_legacy_aliases() -> None:
    config = AmazonOrderQuantityConfig.from_env(
        {
            "AMAZON_REFRESH_TOKEN": "refresh",
            "AMAZON_CLIENT_ID": "client",
            "AMAZON_CLIENT_SECRET": "secret",
        }
    )

    assert config is not None
    assert config.endpoint == DEFAULT_SP_API_ENDPOINT
    assert AmazonOrderQuantityClient.from_env(
        {
            "AMAZON_REFRESH_TOKEN": "refresh",
            "AMAZON_LWA_CLIENT_ID": "client",
            "AMAZON_LWA_CLIENT_SECRET": "secret",
        }
    ).config is not None


@pytest.mark.parametrize("resolver", [lingxing_source_from_args, shipment_source_from_args])
def test_desktop_configuration_values_override_env_path_even_when_empty(resolver) -> None:
    in_memory: dict[str, object] = {}
    args = SimpleNamespace(configuration_values=in_memory, env_path="must-not-be-read.env")

    source = resolver(args)

    assert source is in_memory


@pytest.mark.parametrize("resolver", [lingxing_source_from_args, shipment_source_from_args])
def test_cli_arguments_still_resolve_the_dotenv_path(resolver) -> None:
    args = SimpleNamespace(env_path="custom.env")

    assert resolver(args) == "custom.env"


@pytest.mark.parametrize("resolver", [lingxing_source_from_args, shipment_source_from_args])
def test_configuration_values_must_be_a_mapping(resolver) -> None:
    with pytest.raises(TypeError, match="configuration_values must be a mapping"):
        resolver(SimpleNamespace(configuration_values="not-a-mapping", env_path=".env"))
