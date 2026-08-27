from shipment_automation.erp_mark_policy import (
    AMAZON_MAIN_IMAGE_FORBIDDEN_CHANNEL,
    FORBIDDEN_AMAZON_MAIN_IMAGE_CHANNEL_PATHS,
    amazon_main_image_policy_violation,
)


def _violation(**overrides):
    values = {
        "platform_order_no": "112-1165824-9982644",
        "sales_platform_code": "10001",
        "sales_platform_name": "Amazon",
        "has_main_image": True,
        "carrier": "SpeedX",
        "tracking_no": "SPX123456789012",
        "channel_path": ("手动", "SpeedX（不得标发亚马逊）"),
    }
    values.update(overrides)
    return amazon_main_image_policy_violation(**values)


def test_policy_requires_order_image_tracking_format_and_exact_channel_path():
    violation = _violation()
    assert violation is not None
    assert violation.code == AMAZON_MAIN_IMAGE_FORBIDDEN_CHANNEL

    assert _violation(has_main_image=False) is None
    assert _violation(
        platform_order_no="wc39715",
        sales_platform_code="10002",
        sales_platform_name="WooCommerce",
    ) is None
    assert _violation(tracking_no="9400100000000000000000") is None
    assert _violation(channel_path=()) is None
    assert _violation(channel_path=("手动", "SpeedX")) is None


def test_forbidden_registry_includes_fanyuan_without_inventing_a_tracking_format():
    assert FORBIDDEN_AMAZON_MAIN_IMAGE_CHANNEL_PATHS["FANYUAN"] == (
        "手动",
        "泛远（不得标发亚马逊）",
    )
    # The official public tracker does not publish a stable AWB format.  Until
    # a verified format is configured, route-name evidence alone must not
    # satisfy the policy's double-evidence rule.
    assert _violation(
        carrier="泛远",
        tracking_no="FAR123456789012",
        channel_path=("手动", "泛远（不得标发亚马逊）"),
    ) is None
