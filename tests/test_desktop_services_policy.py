from erp_automation.application.capabilities import (
    Capability as ApiCapability,
    CapabilityMode as ApiCapabilityMode,
)
from erp_automation.application.desktop_services import build_capability_router
from erp_automation.ui.models import CapabilityPolicy


def test_contact_business_mode_is_browser_while_phone_api_remains_diagnostic() -> None:
    router = build_capability_router(
        CapabilityPolicy(emergency_stop_writes=False),
    )

    assert router.mode_for(ApiCapability.UPDATE_BUYER_EMAIL) is ApiCapabilityMode.BROWSER_ONLY
    assert router.mode_for(ApiCapability.UPDATE_PHONE) is ApiCapabilityMode.API_PREFERRED
