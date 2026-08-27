from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .alibaba_logistics import (
    normalize_carrier_name,
    tracking_number_matches_carrier,
)


AMAZON_MAIN_IMAGE_FORBIDDEN_CHANNEL = "amazon_main_image_forbidden_channel"

# These are deliberately the exact ERP cascader paths.  The policy only
# blocks when the carrier/tracking-number evidence and the ERP route agree;
# a carrier label on its own is not enough.
FORBIDDEN_AMAZON_MAIN_IMAGE_CHANNEL_PATHS: dict[str, tuple[str, ...]] = {
    "SPEEDX": ("手动", "SpeedX（不得标发亚马逊）"),
    "FANYUAN": ("手动", "泛远（不得标发亚马逊）"),
    "SWIFTX": ("手动", "SwiftX（不得标发亚马逊）"),
    "1ST": ("手动", "一代国际物流（不得标发亚马逊）"),
}


@dataclass(frozen=True)
class ErpMarkPolicyViolation:
    code: str
    carrier_key: str
    channel_path: tuple[str, ...]
    message: str


def is_amazon_order(
    platform_order_no: object,
    sales_platform_code: object,
    sales_platform_name: object,
) -> bool:
    """Use the same stable Amazon evidence as customer notifications."""

    order_no = str(platform_order_no or "").strip()
    code = str(sales_platform_code or "").strip().casefold()
    name = str(sales_platform_name or "").strip().casefold()
    if code == "10001":
        return True
    if "amazon" in name or "亚马逊" in name:
        return True
    parts = order_no.split("-")
    return (
        len(parts) == 3
        and tuple(len(part) for part in parts) == (3, 7, 7)
        and all(part.isdigit() for part in parts)
    )


def amazon_main_image_policy_violation(
    *,
    platform_order_no: object,
    sales_platform_code: object,
    sales_platform_name: object,
    has_main_image: object,
    carrier: object,
    tracking_no: object,
    channel_path: Sequence[str] | None = None,
) -> ErpMarkPolicyViolation | None:
    """Return a block only when order, image, tracking and route all agree."""

    if not bool(has_main_image) or not is_amazon_order(
        platform_order_no,
        sales_platform_code,
        sales_platform_name,
    ):
        return None
    carrier_key = normalize_carrier_name(str(carrier or ""))
    expected_path = FORBIDDEN_AMAZON_MAIN_IMAGE_CHANNEL_PATHS.get(carrier_key)
    if expected_path is None:
        return None
    selected_path_source = expected_path if channel_path is None else channel_path
    selected_path = tuple(
        str(part or "").strip() for part in selected_path_source
    )
    if selected_path != expected_path:
        return None
    if not tracking_number_matches_carrier(str(carrier or ""), str(tracking_no or "")):
        return None
    route_text = " / ".join(expected_path)
    return ErpMarkPolicyViolation(
        code=AMAZON_MAIN_IMAGE_FORBIDDEN_CHANNEL,
        carrier_key=carrier_key,
        channel_path=expected_path,
        message=(
            "禁止自动标发：该 Amazon 订单包含商品主图，尾程单号已确认符合"
            f" {carrier_key} 格式，且拟选择 ERP 渠道“{route_text}”。"
            "请人工选择允许的承运商，并填写与该承运商格式匹配的正确尾程单号；"
            "校验通过后才可执行出库。"
        ),
    )
