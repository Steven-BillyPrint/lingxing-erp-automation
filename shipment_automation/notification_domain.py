from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from .alibaba_logistics import (
    REAL_OVERSEAS_CARRIER_DISPLAY_NAMES,
    infer_carrier_from_tracking_number,
    normalize_carrier_name,
)

NOTIFICATION_DRAFT = "DRAFT"
NOTIFICATION_AWAITING_REVIEW = "AWAITING_REVIEW"
NOTIFICATION_APPROVED = "APPROVED"
NOTIFICATION_REJECTED = "REJECTED"
NOTIFICATION_SENDING = "SENDING"
NOTIFICATION_ACCEPTED = "ACCEPTED"
NOTIFICATION_DELIVERED = "DELIVERED"
NOTIFICATION_MANUALLY_COMPLETED = "MANUALLY_COMPLETED"
NOTIFICATION_WAITING_CONTACT = "WAITING_CONTACT"
NOTIFICATION_MANUAL_EMAIL_REQUIRED = "MANUAL_EMAIL_REQUIRED"
NOTIFICATION_RETRYABLE = "RETRYABLE"
NOTIFICATION_BLOCKED = "BLOCKED"
NOTIFICATION_FAILED = "FAILED"
NOTIFICATION_CANCELLED = "CANCELLED"
NOTIFICATION_SUPPRESSED = "SUPPRESSED"
NOTIFICATION_DELIVERY_UNCONFIRMED = "DELIVERY_UNCONFIRMED"

CHANNEL_EMAIL = "EMAIL"
CHANNEL_SMS = "SMS"
CHANNEL_MANUAL_EMAIL = "MANUAL_EMAIL"

PLATFORM_POLICY_AMAZON = "AMAZON"
PLATFORM_POLICY_INDEPENDENT_SITE = "INDEPENDENT_SITE"

EMAIL_PRESENCE_UNKNOWN = "UNKNOWN"
EMAIL_PRESENCE_PROVIDED = "PROVIDED"
EMAIL_PRESENCE_NOT_PROVIDED = "NOT_PROVIDED"
CONTACT_SOURCE_CUSTOMIZATION_JSON = "customization_json"
CONTACT_SOURCE_WMS = "lingxing_wms"
CONTACT_SOURCE_LINGXING_ORDER_LIST = "lingxing_order_list"
CONTACT_SOURCE_LINGXING_API_FALLBACK = "lingxing_api_fallback"
CONTACT_SOURCE_LINGXING_DETAIL_REFRESH = "lingxing_order_detail_manual_refresh"
CONTACT_SOURCE_DESKTOP_MANUAL = "desktop_manual"

PHONE_VERIFICATION_UNKNOWN = "UNKNOWN"
PHONE_VERIFICATION_MATCHED = "MATCHED_CUSTOMIZATION_JSON"
PHONE_VERIFICATION_MISSING = "NO_MATCHING_CUSTOMIZATION_PHONE"
PHONE_VERIFICATION_NOT_REQUIRED = "NOT_REQUIRED"

PACKAGE_MANUAL = "MANUAL"
PACKAGE_OVERSEAS_AUTO = "OVERSEAS_AUTO"
PACKAGE_UNKNOWN = "UNKNOWN"

EMAIL_TEMPLATE_VERSION = "shipment-email-v7"
SMS_TEMPLATE_VERSION = "shipment-sms-v7"

INDEPENDENT_SITE_ORDER_RE = re.compile(r"^wc\d+$", re.IGNORECASE)
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
_MISSING_RECIPIENT_NAMES = {"", "-", "--", "n/a", "none", "null", "unknown", "未知"}
_CUSTOMER_CARRIER_ALIASES = {
    "4PX": "4PX",
    "4PXEXPRESS": "4PX",
    "SFINTERNATIONAL": "SF International",
    "SFEXPRESS": "SF Express",
    "YUNEXPRESS": "YunExpress",
    "CHINAPOST": "China Post",
    "JNTEXPRESS": "J&T Express",
    "CAINIAO": "Cainiao",
}
_CUSTOMER_CARRIER_TEXT_ALIASES = (
    ("万邦速达", "Wanb Express"),
    ("万邦", "Wanb Express"),
    ("联邮通", "4PX"),
    ("递四方", "4PX"),
    ("燕文", "Yanwen"),
    ("联邦快递", "FedEx"),
    ("敦豪", "DHL"),
    ("顺丰国际", "SF International"),
    ("顺丰", "SF Express"),
    ("云途", "YunExpress"),
    ("中国邮政", "China Post"),
    ("极兔", "J&T Express"),
    ("菜鸟", "Cainiao"),
)


@dataclass(frozen=True)
class NotificationConfiguration:
    alimail_application_name: str = ""
    alimail_app_id: str = ""
    alimail_app_secret: str = field(default="", repr=False)
    amazon_sender_email: str = "acs@billyprint.com"
    independent_sender_email: str = "cs@billyprint.com"
    sender_display_name: str = "BillyPrint Customer Service"
    clicksend_username: str = field(default="", repr=False)
    clicksend_api_key: str = field(default="", repr=False)
    clicksend_sender_id: str = ""
    amazon_platform_codes: tuple[str, ...] = ("10001",)
    amazon_platform_names: tuple[str, ...] = ("amazon", "亚马逊")
    virtual_email_domains: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "amazon": ("marketplace.amazon.*",),
            "10001": ("marketplace.amazon.*",),
        }
    )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "NotificationConfiguration":
        raw_domains = values.get("notifications.virtual_email_domains", {})
        if isinstance(raw_domains, str):
            try:
                raw_domains = json.loads(raw_domains)
            except json.JSONDecodeError:
                raw_domains = {}
        domains: dict[str, tuple[str, ...]] = {}
        if isinstance(raw_domains, Mapping):
            for key, value in raw_domains.items():
                items = value if isinstance(value, (list, tuple, set)) else (value,)
                cleaned = tuple(
                    str(item or "").strip().lower().lstrip("@.")
                    for item in items
                    if str(item or "").strip()
                )
                if cleaned:
                    domains[str(key or "").strip().lower()] = cleaned
        if not domains:
            domains = {
                "amazon": ("marketplace.amazon.*",),
                "10001": ("marketplace.amazon.*",),
            }

        def _tuple(key: str, default: Sequence[str]) -> tuple[str, ...]:
            raw = values.get(key, default)
            if isinstance(raw, str):
                raw = re.split(r"[,;|\n]", raw)
            if not isinstance(raw, (list, tuple, set)):
                raw = default
            return tuple(str(item).strip().lower() for item in raw if str(item).strip())

        return cls(
            alimail_application_name=str(values.get("alimail.application_name") or ""),
            alimail_app_id=str(values.get("alimail.app_id") or ""),
            alimail_app_secret=str(values.get("alimail.app_secret") or ""),
            amazon_sender_email=str(
                values.get("alimail.amazon_sender_email") or "acs@billyprint.com"
            ),
            independent_sender_email=str(
                values.get("alimail.independent_sender_email") or "cs@billyprint.com"
            ),
            sender_display_name=str(
                values.get("alimail.sender_display_name")
                or "BillyPrint Customer Service"
            ),
            clicksend_username=str(values.get("clicksend.username") or ""),
            clicksend_api_key=str(values.get("clicksend.api_key") or ""),
            clicksend_sender_id=str(values.get("clicksend.sender_id") or ""),
            amazon_platform_codes=_tuple(
                "notifications.amazon_platform_codes", ("10001",)
            ),
            amazon_platform_names=_tuple(
                "notifications.amazon_platform_names", ("amazon", "亚马逊")
            ),
            virtual_email_domains=domains,
        )


@dataclass(frozen=True)
class OrderContact:
    platform_order_no: str
    recipient_name: str = ""
    email: str = ""
    email_presence: str = EMAIL_PRESENCE_UNKNOWN
    phone_raw: str = ""
    sales_platform_code: str = ""
    sales_platform_name: str = ""
    store_name: str = ""
    site_name: str = ""
    source: str = "lingxing_order"
    recipient_name_source: str = ""
    email_source: str = ""
    phone_source: str = ""
    verified_phone_e164: str = ""
    phone_verification_state: str = PHONE_VERIFICATION_UNKNOWN
    captured_at: str = ""
    system_order_nos: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderProductSnapshot:
    platform_order_no: str
    system_order_no: str
    item_key: str
    source_sequence: int = 0
    local_sku: str = ""
    raw_title: str = ""
    display_title: str = ""
    has_main_image: bool = False
    metadata_valid: bool = True
    is_instruction: bool = False
    source_payload_hash: str = ""


@dataclass(frozen=True)
class PackageSnapshot:
    package_key: str
    platform_order_no: str
    system_order_no: str
    shipment_type: str
    carrier_raw: str = ""
    carrier: str = ""
    waybill_no: str = ""
    tracking_no: str = ""
    final_tracking_no: str = ""
    stable_sequence: int = 0
    stable_label: str = ""
    source_payload_hash: str = ""
    customer_visible: bool = True
    visibility_reason: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.carrier.strip() and self.final_tracking_no.strip())


@dataclass(frozen=True)
class RenderedNotification:
    platform_order_no: str
    channel: str | None
    recipient_name: str
    recipient_email: str
    recipient_phone: str
    target: str
    sender_email: str
    subject: str
    body: str
    body_html: str
    template_version: str
    content_hash: str
    package_total: int
    package_complete: int
    package_missing: int
    product_names: tuple[str, ...] = ()
    sms_encoding: str = ""
    sms_character_count: int = 0
    sms_segment_count: int = 0
    blocked_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductAnalysis:
    product_names: tuple[str, ...]
    instruction_system_order_nos: tuple[str, ...]
    blocked_reasons: tuple[str, ...]


def normalize_product_sku(value: str | None) -> str:
    return "".join(str(value or "").split()).casefold()


def shorten_product_title(value: str | None) -> str:
    text = re.split(r"[|｜,，]", str(value or ""), maxsplit=1)[0]
    text = re.sub(r"\s+", " ", text.strip())
    text = re.sub(r"^BillyPrint(?:\s+|$)", "", text, flags=re.IGNORECASE).strip()
    words = text.split()[:5]
    trailing_prepositions = {"with", "for", "of", "to", "in", "on", "at", "by", "from"}
    while words and words[-1].casefold() in trailing_prepositions:
        words.pop()
    return " ".join(words)


def analyze_order_products(
    products: Sequence[OrderProductSnapshot],
    *,
    expected_system_order_nos: Sequence[str] = (),
) -> ProductAnalysis:
    expected = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in expected_system_order_nos
            if str(value or "").strip()
        )
    )
    by_system: dict[str, list[OrderProductSnapshot]] = {}
    for product in products:
        system_order_no = product.system_order_no.strip()
        if system_order_no:
            by_system.setdefault(system_order_no, []).append(product)

    blocked: list[str] = []
    if any(not product.metadata_valid for product in products):
        blocked.append("product_data_invalid")
    if any(system_order_no not in by_system for system_order_no in expected):
        blocked.append("product_items_missing")

    instruction_systems: list[str] = []
    for system_order_no, system_products in by_system.items():
        instruction_flags = {bool(product.is_instruction) for product in system_products}
        if instruction_flags == {True}:
            instruction_systems.append(system_order_no)

    names: list[str] = []
    seen_names: set[str] = set()
    image_products = [product for product in products if product.has_main_image]
    if image_products:
        if any(not product.display_title.strip() for product in image_products):
            blocked.append("product_title_missing")
        candidate_names = [product.display_title.strip() for product in image_products]
    else:
        # Some platforms do not expose snapshot_image through the order-list
        # API. The order title remains useful customer-facing context; use the
        # physical SKU only when that title is also absent.
        candidate_names = [
            (
                product.display_title.strip()
                or re.sub(r"\s+", " ", product.local_sku).strip()
            )
            for product in products
            if not product.is_instruction
        ]
        if not any(candidate_names):
            blocked.append("product_sku_missing")

    for name in candidate_names:
        key = name.casefold()
        if name and key not in seen_names:
            names.append(name)
            seen_names.add(key)

    return ProductAnalysis(
        product_names=tuple(names),
        instruction_system_order_nos=tuple(dict.fromkeys(instruction_systems)),
        blocked_reasons=tuple(dict.fromkeys(blocked)),
    )


def stable_package_label(sequence: int) -> str:
    """Return spreadsheet-style lower-case labels: a..z, aa, ab..."""

    if sequence <= 0:
        raise ValueError("Package sequence must be positive.")
    output = ""
    current = sequence
    while current:
        current, remainder = divmod(current - 1, 26)
        output = chr(ord("a") + remainder) + output
    return output


def normalize_phone(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if _E164_RE.fullmatch(text):
        return text
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        candidate = f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = f"+{digits}"
    else:
        return None
    return candidate if _E164_RE.fullmatch(candidate) else None


def normalize_email(value: str | None) -> str | None:
    _display_name, address = parseaddr(str(value or "").strip())
    address = address.strip().lower()
    return address if address and _EMAIL_RE.fullmatch(address) else None


def normalize_recipient_name(value: str | None) -> str:
    text = str(value or "").strip()
    return "" if text.casefold() in _MISSING_RECIPIENT_NAMES else text


def is_amazon_platform(
    platform_order_no: str,
    platform_code: str,
    platform_name: str,
    configuration: NotificationConfiguration,
) -> bool:
    code = str(platform_code or "").strip().lower()
    name = str(platform_name or "").strip().lower()
    if code and code in configuration.amazon_platform_codes:
        return True
    if any(marker and marker in name for marker in configuration.amazon_platform_names):
        return True
    return bool(re.fullmatch(r"\d{3}-\d{7}-\d{7}", platform_order_no.strip()))


def is_independent_site_order(platform_order_no: str) -> bool:
    return bool(INDEPENDENT_SITE_ORDER_RE.fullmatch(str(platform_order_no or "").strip()))


def notification_platform_policy(
    contact: OrderContact,
    configuration: NotificationConfiguration,
) -> str | None:
    if is_independent_site_order(contact.platform_order_no):
        return PLATFORM_POLICY_INDEPENDENT_SITE
    if is_amazon_platform(
        contact.platform_order_no,
        contact.sales_platform_code,
        contact.sales_platform_name,
        configuration,
    ):
        return PLATFORM_POLICY_AMAZON
    return None


def is_virtual_email(
    email: str,
    *,
    platform_code: str,
    platform_name: str,
    configuration: NotificationConfiguration,
) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    # This is a non-overridable safety rule.  Amazon uses country-specific
    # relay domains (for example .ca and .co.uk), and limiting the check to
    # marketplace.amazon.com allowed those aliases to receive real e-mail.
    if domain.startswith("marketplace.amazon.") and domain.removeprefix(
        "marketplace.amazon."
    ):
        return True
    keys = {
        str(platform_code or "").strip().lower(),
        str(platform_name or "").strip().lower(),
    }
    for key, domains in configuration.virtual_email_domains.items():
        normalized_key = str(key).strip().lower()
        if normalized_key not in keys and not any(
            normalized_key and normalized_key in candidate for candidate in keys
        ):
            continue
        if any(
            (
                item.endswith(".*")
                and domain.startswith(f"{item[:-2]}.")
                and domain != f"{item[:-2]}."
            )
            or domain == item
            or domain.endswith(f".{item}")
            for item in domains
        ):
            return True
    return False


def select_sender_email(
    contact: OrderContact,
    configuration: NotificationConfiguration,
    *,
    platform_policy: str | None = None,
) -> str | None:
    policy = platform_policy or notification_platform_policy(contact, configuration)
    if policy == PLATFORM_POLICY_INDEPENDENT_SITE:
        return normalize_email(configuration.independent_sender_email)
    if policy == PLATFORM_POLICY_AMAZON:
        return normalize_email(configuration.amazon_sender_email)
    return None


def select_channel(
    contact: OrderContact,
    configuration: NotificationConfiguration,
    *,
    platform_policy: str | None = None,
) -> tuple[str | None, str, str]:
    policy = platform_policy or notification_platform_policy(contact, configuration)
    email_presence = str(contact.email_presence or EMAIL_PRESENCE_UNKNOWN).strip().upper()
    email = normalize_email(contact.email)
    phone = normalize_phone(contact.phone_raw)
    if (
        email_presence != EMAIL_PRESENCE_NOT_PROVIDED
        and email
        and not is_virtual_email(
            email,
            platform_code=contact.sales_platform_code,
            platform_name=contact.sales_platform_name,
            configuration=configuration,
        )
    ):
        return CHANNEL_EMAIL, email, phone or ""
    if policy == PLATFORM_POLICY_INDEPENDENT_SITE:
        return (CHANNEL_SMS, email or "", phone) if phone else (None, email or "", "")
    verified_phone = normalize_phone(contact.verified_phone_e164)
    phone_is_json_verified = bool(
        verified_phone
        and phone == verified_phone
        and str(contact.phone_verification_state or "").strip().upper()
        == PHONE_VERIFICATION_MATCHED
    )
    if policy == PLATFORM_POLICY_AMAZON and phone and phone_is_json_verified:
        return CHANNEL_SMS, email or "", phone
    if (
        email
        and policy == PLATFORM_POLICY_AMAZON
        and is_virtual_email(
            email,
            platform_code=contact.sales_platform_code,
            platform_name=contact.sales_platform_name,
            configuration=configuration,
        )
    ):
        return CHANNEL_MANUAL_EMAIL, email, phone or ""
    return None, email or "", ""


def render_package_lines(
    packages: Iterable[PackageSnapshot],
    *,
    include_available_soon: bool | None = None,
) -> str:
    ordered = sorted(
        (item for item in packages if item.customer_visible),
        key=lambda item: item.stable_sequence,
    )
    lines = [
        f"· Package {stable_package_label(display_index)}: "
        f"{customer_carrier_display_name(item.carrier, item.final_tracking_no)} "
        f"{item.final_tracking_no.strip()}"
        for display_index, item in enumerate(
            (candidate for candidate in ordered if candidate.complete),
            start=1,
        )
    ]
    has_incomplete = any(not item.complete for item in ordered)
    if has_incomplete if include_available_soon is None else include_available_soon:
        lines.append("· Available soon.")
    return "\n".join(lines)


def customer_carrier_display_name(
    carrier: str | None,
    tracking_no: str | None = None,
) -> str:
    """Return an English-only carrier label safe for customer messages."""

    raw = " ".join(str(carrier or "").strip().split())
    for marker, display in _CUSTOMER_CARRIER_TEXT_ALIASES:
        if marker in raw:
            return display

    normalized = normalize_carrier_name(raw)
    if normalized in REAL_OVERSEAS_CARRIER_DISPLAY_NAMES:
        return REAL_OVERSEAS_CARRIER_DISPLAY_NAMES[normalized]
    if normalized in _CUSTOMER_CARRIER_ALIASES:
        return _CUSTOMER_CARRIER_ALIASES[normalized]

    folded = raw.casefold()
    named_patterns = (
        (r"(?:^|[^a-z])wanb(?:[ -]?express)?(?:$|[^a-z])", "Wanb Express"),
        (r"(?:^|[^a-z])fedex(?:$|[^a-z])", "FedEx"),
        (r"(?:^|[^a-z])ups(?:$|[^a-z])", "UPS"),
        (r"(?:^|[^a-z])usps(?:$|[^a-z])", "USPS"),
        (r"(?:^|[^a-z])dhl(?:$|[^a-z])", "DHL"),
        (r"(?:^|[^a-z])4px(?:$|[^a-z])", "4PX"),
        (r"(?:^|[^a-z])yanwen(?:$|[^a-z])", "Yanwen"),
        (r"(?:^|[^a-z])sf[ -]?international(?:$|[^a-z])", "SF International"),
    )
    for pattern, display in named_patterns:
        if re.search(pattern, folded):
            return display

    tracking = str(tracking_no or "").strip()
    if tracking.upper().startswith("4PX"):
        return "4PX"
    inferred = infer_carrier_from_tracking_number(tracking)
    if inferred:
        return inferred

    if raw and raw.isascii() and re.search(r"[A-Za-z0-9]", raw):
        return raw
    return "International Carrier"


def _carrier_tracking_family(carrier: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", str(carrier or "").casefold())
    aliases = {
        "fedex": {"fedex", "federalexpress"},
        "ups": {"ups", "unitedparcelservice"},
        "usps": {"usps", "unitedstatespostalservice"},
        "dhl": {"dhl", "dhlexpress", "dhlecommerce"},
        "gofo": {"gofo", "gofoexpress"},
        "yanwen": {"yanwen", "yanwenexpress", "ywe"},
        "speedx": {"speedx", "speedxexpress"},
        "uniuni": {"uniuni", "uni", "uniexpress"},
        "1st": {"1st", "1stgroup"},
        "swiftx": {"swiftx", "swiftxexpress"},
        "wanb": {"wanb", "wanbexpress"},
    }
    for family, values in aliases.items():
        if normalized in values:
            return family
    return "17track"


def tracking_url_for(carrier: str | None, tracking_no: str | None) -> str:
    """Return a deterministic allow-listed HTTPS tracking URL."""

    number = str(tracking_no or "").strip()
    if not number:
        return ""
    encoded = quote(number, safe="")
    family = _carrier_tracking_family(carrier)
    if family == "fedex":
        return f"https://www.fedex.com/fedextrack/?trknbr={encoded}&locale=en_US"
    if family == "ups":
        return f"https://www.ups.com/track?loc=en_US&tracknum={encoded}"
    if family == "usps":
        return f"https://tools.usps.com/go/TrackConfirmAction?tLabels={encoded}"
    if family == "dhl":
        return f"https://www.dhl.com/global-en/home/tracking.html?tracking-id={encoded}"
    if family == "gofo":
        return f"https://www.gofoexpress.com/tracking.html?searchID={encoded}"
    if family == "yanwen":
        return f"https://track.yw56.com.cn/en/querydel?nums={encoded}"
    if family == "speedx":
        return f"https://tracking.speedx.io/{encoded}"
    if family == "uniuni":
        return f"https://www.uniuni.com/tracking/?no={encoded}"
    if family == "swiftx":
        return f"https://swiftx-express.com/track?trackingNumber={encoded}"
    if family == "wanb":
        return f"https://tracking.wanbexpress.com/?trackingNumbers={encoded}"
    return f"https://www.17track.net/en/track?nums={encoded}"


def render_sms_package_lines(
    packages: Iterable[PackageSnapshot],
    *,
    include_available_soon: bool | None = None,
) -> str:
    ordered = sorted(
        (item for item in packages if item.customer_visible),
        key=lambda item: item.stable_sequence,
    )
    lines: list[str] = []
    for display_index, item in enumerate(
        (candidate for candidate in ordered if candidate.complete),
        start=1,
    ):
        lines.append(
            f"· Package {stable_package_label(display_index)}: "
            f"{customer_carrier_display_name(item.carrier, item.final_tracking_no)} "
            f"{item.final_tracking_no.strip()}"
        )
        lines.append(
            f"  Track: {tracking_url_for(item.carrier, item.final_tracking_no)}"
        )
    has_incomplete = any(not item.complete for item in ordered)
    if has_incomplete if include_available_soon is None else include_available_soon:
        lines.append("· Available soon.")
    return "\n".join(lines)


def render_email_package_lines_html(
    packages: Iterable[PackageSnapshot],
    *,
    include_available_soon: bool | None = None,
) -> str:
    ordered = sorted(
        (item for item in packages if item.customer_visible),
        key=lambda item: item.stable_sequence,
    )
    lines: list[str] = []
    for display_index, item in enumerate(
        (candidate for candidate in ordered if candidate.complete),
        start=1,
    ):
        url = tracking_url_for(item.carrier, item.final_tracking_no)
        label = html.escape(stable_package_label(display_index))
        carrier = html.escape(
            customer_carrier_display_name(item.carrier, item.final_tracking_no)
        )
        number = html.escape(item.final_tracking_no.strip())
        href = html.escape(url, quote=True)
        lines.append(
            f"· Package {label}: {carrier} "
            f'<a href="{href}" target="_blank" rel="noopener noreferrer">'
            f"{number}</a>"
        )
    has_incomplete = any(not item.complete for item in ordered)
    if has_incomplete if include_available_soon is None else include_available_soon:
        lines.append("· Available soon.")
    return "<br>".join(lines)


_GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENDED = set("^{}\\[~]|€")


def sms_metrics(body: str) -> tuple[str, int, int]:
    if all(character in _GSM7_BASIC or character in _GSM7_EXTENDED for character in body):
        units = sum(2 if character in _GSM7_EXTENDED else 1 for character in body)
        segments = 1 if units <= 160 else (units + 152) // 153
        return "GSM-7", units, segments
    units = len(body.encode("utf-16-be")) // 2
    segments = 1 if units <= 70 else (units + 66) // 67
    return "Unicode", units, segments


def render_notification(
    contact: OrderContact,
    packages: Sequence[PackageSnapshot],
    configuration: NotificationConfiguration,
    *,
    expected_system_order_nos: Sequence[str] | None = None,
    products: Sequence[OrderProductSnapshot] | None = None,
    platform_policy: str | None = None,
) -> RenderedNotification:
    ordered = sorted(packages, key=lambda item: item.stable_sequence)
    recipient_name = normalize_recipient_name(contact.recipient_name)
    policy = platform_policy or notification_platform_policy(contact, configuration)
    channel, email, phone = select_channel(
        contact,
        configuration,
        platform_policy=policy,
    )
    expected_systems = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in (
                contact.system_order_nos
                if expected_system_order_nos is None
                else expected_system_order_nos
            )
            if str(value or "").strip()
        )
    )
    product_analysis = (
        analyze_order_products(products, expected_system_order_nos=expected_systems)
        if products is not None
        else ProductAnalysis((), (), ())
    )
    instruction_systems = set(product_analysis.instruction_system_order_nos)
    customer_packages = [
        item
        for item in ordered
        if item.customer_visible and item.system_order_no.strip() not in instruction_systems
    ]
    customer_expected_systems = tuple(
        system_order_no
        for system_order_no in expected_systems
        if system_order_no not in instruction_systems
    )
    observed_systems = {
        item.system_order_no.strip()
        for item in customer_packages
        if item.system_order_no.strip()
    }
    uncovered_systems = tuple(
        system_order_no
        for system_order_no in customer_expected_systems
        if system_order_no not in observed_systems
    )
    package_total = len(customer_packages) + len(uncovered_systems)
    complete = sum(1 for item in customer_packages if item.complete)
    missing = package_total - complete
    sender = (
        select_sender_email(contact, configuration, platform_policy=policy)
        if channel == CHANNEL_EMAIL
        else ""
    )
    blocked: list[str] = []
    if not recipient_name:
        blocked.append("recipient_name_missing")
    blocked.extend(product_analysis.blocked_reasons)
    if not customer_packages:
        blocked.append("packages_missing")
    elif complete == 0:
        blocked.append("all_tracking_missing")
    if any(
        item.shipment_type == PACKAGE_UNKNOWN
        and item.visibility_reason != "pending_wms"
        for item in customer_packages
    ):
        blocked.append("package_type_unknown")
    if channel is None:
        blocked.append("recipient_contact_unavailable")
    if channel == CHANNEL_EMAIL and not sender:
        blocked.append("sender_account_unconfigured")

    subject = ""
    sms_encoding = ""
    sms_characters = 0
    sms_segments = 0
    body_html = ""
    product_names = product_analysis.product_names
    if len(product_names) == 1:
        product_block = f"Product: {product_names[0]}"
        product_block_html = f"Product: {html.escape(product_names[0])}"
    elif product_names:
        product_block = "Products:\n" + "\n".join(f"· {name}" for name in product_names)
        product_block_html = "Products:<br>" + "<br>".join(
            f"· {html.escape(name)}" for name in product_names
        )
    else:
        product_block = ""
        product_block_html = ""
    product_text_section = f"{product_block}\n\n" if product_block else ""
    product_html_section = f"{product_block_html}<br><br>" if product_block_html else ""
    if channel in {CHANNEL_EMAIL, CHANNEL_MANUAL_EMAIL}:
        lines = render_package_lines(
            customer_packages,
            include_available_soon=missing > 0,
        )
        subject = f"Shipment Update - {contact.platform_order_no}"
        body = (
            f"Dear {recipient_name},\n\n"
            f"{product_text_section}"
            "We would like to inform you that your order has been divided into "
            f"{package_total} separate shipments for better processing.\n\n"
            f"{lines}\n\n"
            "You can track the status of your package directly on the carrier's "
            "official website using your tracking number.\n\n"
            "We sincerely appreciate your understanding and patience throughout "
            "this process. Your satisfaction is always our top priority. If you "
            "have any further questions or need assistance, please don't hesitate "
            "to contact us — we're always here to help.\n\n"
            "Best Regards,\nBillyPrint Customer Service"
        )
        package_lines_html = render_email_package_lines_html(
            customer_packages,
            include_available_soon=missing > 0,
        )
        body_html = (
            f"Dear {html.escape(recipient_name)},<br><br>"
            f"{product_html_section}"
            "We would like to inform you that your order has been divided into "
            f"{package_total} separate shipments for better processing.<br><br>"
            f"{package_lines_html}<br><br>"
            "You can track the status of your package directly on the carrier's "
            "official website using your tracking number.<br><br>"
            "We sincerely appreciate your understanding and patience throughout "
            "this process. Your satisfaction is always our top priority. If you "
            "have any further questions or need assistance, please don't hesitate "
            "to contact us — we're always here to help.<br><br>"
            "Best Regards,<br>BillyPrint Customer Service"
        )
        template_version = EMAIL_TEMPLATE_VERSION
        target = email
    else:
        lines = render_sms_package_lines(
            customer_packages,
            include_available_soon=missing > 0,
        )
        body = (
            f"Dear {recipient_name},\n\n"
            f"{product_text_section}"
            "Thank you for your order. For better processing, your order has been "
            "divided into separate shipments:\n\n"
            f"{lines}\n\n"
            "You can track your packages on the carrier’s official website using "
            "the tracking numbers provided.\n\n"
            "Thank you for your patience and understanding.\n\n"
            "Best Regards,\nBillyPrint Customer Service"
        )
        template_version = SMS_TEMPLATE_VERSION
        target = phone
        sms_encoding, sms_characters, sms_segments = sms_metrics(body)

    payload = {
        "platform_order_no": contact.platform_order_no,
        "recipient_name": recipient_name,
        "recipient_email": email,
        "email_presence": str(
            contact.email_presence or EMAIL_PRESENCE_UNKNOWN
        ).strip().upper(),
        "recipient_phone": phone,
        "contact_source": contact.source,
        "recipient_name_source": contact.recipient_name_source,
        "email_source": contact.email_source,
        "phone_source": contact.phone_source,
        "verified_phone_e164": contact.verified_phone_e164,
        "phone_verification_state": contact.phone_verification_state,
        "platform_code": contact.sales_platform_code,
        "platform_name": contact.sales_platform_name,
        "platform_policy": policy or "",
        "store_name": contact.store_name,
        "site_name": contact.site_name,
        "channel": channel,
        "target": target,
        "sender_email": sender or "",
        "subject": subject,
        "body": body,
        "body_html": body_html,
        "template_version": template_version,
        "package_total": package_total,
        "package_complete": complete,
        "package_missing": missing,
        # Preserve which expected systems are still waiting for WMS.  The
        # customer wording intentionally stays generic, but an operator must
        # review a new immutable snapshot when the pending system identity
        # changes even if the pending count happens to stay the same.
        "pending_system_order_nos": list(uncovered_systems),
        "product_names": list(product_names),
        "product_facts": [
            {
                "system_order_no": product.system_order_no,
                "item_key": product.item_key,
                "source_sequence": product.source_sequence,
                "local_sku": product.local_sku,
                "raw_title": product.raw_title,
                "display_title": product.display_title,
                "has_main_image": product.has_main_image,
                "metadata_valid": product.metadata_valid,
                "is_instruction": product.is_instruction,
                "source_payload_hash": product.source_payload_hash,
            }
            for product in products or ()
        ],
        # Only customer-visible logistics belong in the approval hash.  WMS may
        # expose an incomplete row before its tracking number is ready; that
        # internal transition must not invalidate a reviewed notification.
        "packages": [
            {
                "key": item.package_key,
                "sequence": item.stable_sequence,
                "label": stable_package_label(display_index),
                "type": item.shipment_type,
                "carrier": customer_carrier_display_name(
                    item.carrier,
                    item.final_tracking_no,
                ),
                "waybill_no": item.waybill_no,
                "tracking_no": item.tracking_no,
                "final_tracking_no": item.final_tracking_no,
                "tracking_url": (
                    tracking_url_for(item.carrier, item.final_tracking_no)
                    if item.complete
                    else ""
                ),
            }
            for display_index, item in enumerate(
                (
                    candidate
                    for candidate in customer_packages
                    if candidate.complete
                ),
                start=1,
            )
        ],
    }
    content_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return RenderedNotification(
        platform_order_no=contact.platform_order_no,
        channel=channel,
        recipient_name=recipient_name,
        recipient_email=email,
        recipient_phone=phone,
        target=target,
        sender_email=sender or "",
        subject=subject,
        body=body,
        body_html=body_html,
        template_version=template_version,
        content_hash=content_hash,
        package_total=package_total,
        package_complete=complete,
        package_missing=missing,
        product_names=product_names,
        sms_encoding=sms_encoding,
        sms_character_count=sms_characters,
        sms_segment_count=sms_segments,
        blocked_reasons=tuple(blocked),
    )


__all__ = [
    "CHANNEL_EMAIL",
    "CHANNEL_MANUAL_EMAIL",
    "CHANNEL_SMS",
    "CONTACT_SOURCE_CUSTOMIZATION_JSON",
    "CONTACT_SOURCE_DESKTOP_MANUAL",
    "CONTACT_SOURCE_LINGXING_API_FALLBACK",
    "CONTACT_SOURCE_LINGXING_ORDER_LIST",
    "CONTACT_SOURCE_LINGXING_DETAIL_REFRESH",
    "CONTACT_SOURCE_WMS",
    "EMAIL_PRESENCE_NOT_PROVIDED",
    "EMAIL_PRESENCE_PROVIDED",
    "EMAIL_PRESENCE_UNKNOWN",
    "EMAIL_TEMPLATE_VERSION",
    "SMS_TEMPLATE_VERSION",
    "NOTIFICATION_ACCEPTED",
    "NOTIFICATION_APPROVED",
    "NOTIFICATION_AWAITING_REVIEW",
    "NOTIFICATION_BLOCKED",
    "NOTIFICATION_CANCELLED",
    "NOTIFICATION_DELIVERED",
    "NOTIFICATION_DELIVERY_UNCONFIRMED",
    "NOTIFICATION_DRAFT",
    "NOTIFICATION_FAILED",
    "NOTIFICATION_MANUAL_EMAIL_REQUIRED",
    "NOTIFICATION_REJECTED",
    "NOTIFICATION_RETRYABLE",
    "NOTIFICATION_SENDING",
    "NOTIFICATION_SUPPRESSED",
    "NOTIFICATION_WAITING_CONTACT",
    "NotificationConfiguration",
    "OrderContact",
    "OrderProductSnapshot",
    "PACKAGE_MANUAL",
    "PACKAGE_OVERSEAS_AUTO",
    "PACKAGE_UNKNOWN",
    "PHONE_VERIFICATION_MATCHED",
    "PHONE_VERIFICATION_MISSING",
    "PHONE_VERIFICATION_NOT_REQUIRED",
    "PHONE_VERIFICATION_UNKNOWN",
    "PLATFORM_POLICY_AMAZON",
    "PLATFORM_POLICY_INDEPENDENT_SITE",
    "PackageSnapshot",
    "ProductAnalysis",
    "RenderedNotification",
    "analyze_order_products",
    "customer_carrier_display_name",
    "is_independent_site_order",
    "is_virtual_email",
    "notification_platform_policy",
    "normalize_email",
    "normalize_phone",
    "normalize_product_sku",
    "normalize_recipient_name",
    "render_notification",
    "render_email_package_lines_html",
    "render_package_lines",
    "render_sms_package_lines",
    "select_channel",
    "select_sender_email",
    "sms_metrics",
    "shorten_product_title",
    "stable_package_label",
    "tracking_url_for",
]
