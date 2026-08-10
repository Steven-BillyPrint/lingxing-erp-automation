from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

import shipment_automation.notification_store as notification_store_module
from erp_automation.application.desktop_tasks import DesktopTaskRunner
from erp_automation.ui.controller import ControlResult
from erp_automation.ui.persistent_controller import PersistentBackgroundTaskController
from erp_automation.ui.models import DesktopSettings
from shipment_automation.notification_domain import (
    CHANNEL_EMAIL,
    CHANNEL_MANUAL_EMAIL,
    CHANNEL_SMS,
    CONTACT_SOURCE_CUSTOMIZATION_JSON,
    CONTACT_SOURCE_DESKTOP_MANUAL,
    CONTACT_SOURCE_LINGXING_ORDER_LIST,
    CONTACT_SOURCE_WMS,
    EMAIL_PRESENCE_NOT_PROVIDED,
    EMAIL_PRESENCE_PROVIDED,
    NOTIFICATION_AWAITING_REVIEW,
    NOTIFICATION_BLOCKED,
    NOTIFICATION_CANCELLED,
    NOTIFICATION_DELIVERY_UNCONFIRMED,
    NOTIFICATION_MANUALLY_COMPLETED,
    NOTIFICATION_MANUAL_EMAIL_REQUIRED,
    NOTIFICATION_SUPPRESSED,
    NOTIFICATION_WAITING_CONTACT,
    PHONE_VERIFICATION_MATCHED,
    PHONE_VERIFICATION_MISSING,
    NotificationConfiguration,
    OrderContact,
    PACKAGE_MANUAL,
    PACKAGE_OVERSEAS_AUTO,
    OrderProductSnapshot,
    PackageSnapshot,
    analyze_order_products,
    customer_carrier_display_name,
    normalize_phone,
    render_notification,
    shorten_product_title,
    stable_package_label,
    tracking_url_for,
)
from shipment_automation.notification_providers import (
    AlimailClient,
    ClickSendClient,
    NotificationProviderError,
)
from shipment_automation.notification_providers import ProviderAcceptance
from shipment_automation.notification_service import ShipmentNotificationService
from shipment_automation.notification_store import (
    NotificationStateError,
    ShipmentNotificationStore,
    StaleNotificationError,
)
from shipment_automation.queue_store import SCHEMA_VERSION, ShipmentWorkflowStore
from shipment_automation.models import ShipmentCandidate
from shipment_automation.notification_sync import (
    _discover_recent_amazon_orders,
    is_terminal_wms_row,
    package_from_wms_row,
    sync_notification_drafts,
)


def _config() -> NotificationConfiguration:
    return NotificationConfiguration(
        alimail_application_name="mail-app",
        alimail_app_id="app-id",
        alimail_app_secret="secret",
        clicksend_username="username",
        clicksend_api_key="key",
    )


def _contact(**overrides: Any) -> OrderContact:
    values = {
        "platform_order_no": "112-1234567-1234567",
        "recipient_name": "Customer",
        "email": "customer@example.com",
        "email_presence": EMAIL_PRESENCE_PROVIDED,
        "phone_raw": "+14155552671",
        "sales_platform_code": "10001",
        "sales_platform_name": "Amazon",
        "source": CONTACT_SOURCE_CUSTOMIZATION_JSON,
        "recipient_name_source": CONTACT_SOURCE_WMS,
        "email_source": CONTACT_SOURCE_CUSTOMIZATION_JSON,
        "phone_source": CONTACT_SOURCE_CUSTOMIZATION_JSON,
        "verified_phone_e164": "+14155552671",
        "phone_verification_state": PHONE_VERIFICATION_MATCHED,
        "system_order_nos": ("10001",),
    }
    values.update(overrides)
    return OrderContact(**values)


def _package(sequence: int, *, complete: bool = True) -> PackageSnapshot:
    return PackageSnapshot(
        package_key=f"1000{sequence}:WO-{sequence}",
        platform_order_no="112-1234567-1234567",
        system_order_no=f"1000{sequence}",
        shipment_type=PACKAGE_MANUAL,
        carrier_raw="FedEx" if complete else "",
        carrier="FedEx" if complete else "",
        waybill_no=f"TRACK-{sequence}" if complete else "",
        tracking_no=f"ALS-{sequence}",
        final_tracking_no=f"TRACK-{sequence}" if complete else "",
        stable_sequence=sequence,
        stable_label=stable_package_label(sequence),
    )


def _products(system_count: int = 1) -> list[OrderProductSnapshot]:
    return [
        OrderProductSnapshot(
            platform_order_no="112-1234567-1234567",
            system_order_no=f"1000{index}",
            item_key=f"ITEM-{index}",
            local_sku=f"PRODUCT-{index}",
            raw_title=("Test Product | Keywords" if index == 1 else ""),
            display_title=("Test Product" if index == 1 else ""),
            has_main_image=index == 1,
            source_payload_hash=f"HASH-{index}",
        )
        for index in range(1, system_count + 1)
    ]


def test_contact_channel_and_phone_rules() -> None:
    assert normalize_phone("415-555-2671") == "+14155552671"
    assert normalize_phone("14155552671") == "+14155552671"
    assert normalize_phone("123") is None

    email = render_notification(_contact(), [_package(1)], _config())
    assert email.channel == CHANNEL_EMAIL
    assert email.sender_email == "acs@billyprint.com"

    virtual = render_notification(
        _contact(email="alias@marketplace.amazon.com"), [_package(1)], _config()
    )
    assert virtual.channel == CHANNEL_SMS

    unverified = render_notification(
        _contact(
            email="alias@marketplace.amazon.com",
            phone_raw="+1 619-854-2705 ext. 01508",
            verified_phone_e164="",
            phone_verification_state=PHONE_VERIFICATION_MISSING,
        ),
        [_package(1)],
        _config(),
    )
    assert unverified.channel == CHANNEL_MANUAL_EMAIL
    assert virtual.target == "+14155552671"

    for country_domain in (
        "alias@marketplace.amazon.ca",
        "alias@marketplace.amazon.co.uk",
        "ALIAS@MARKETPLACE.AMAZON.COM.MX",
    ):
        country_virtual = render_notification(
            _contact(email=country_domain), [_package(1)], _config()
        )
        assert country_virtual.channel == CHANNEL_SMS
        assert country_virtual.target == "+14155552671"

    virtual_without_phone = render_notification(
        _contact(email="alias@marketplace.amazon.ca", phone_raw=""),
        [_package(1)],
        _config(),
    )
    assert virtual_without_phone.channel == CHANNEL_MANUAL_EMAIL

    blank = render_notification(_contact(email=""), [_package(1)], _config())
    assert blank.channel == CHANNEL_SMS

    explicitly_not_provided = render_notification(
        _contact(
            email="customer@example.com",
            email_presence=EMAIL_PRESENCE_NOT_PROVIDED,
        ),
        [_package(1)],
        _config(),
    )
    assert explicitly_not_provided.channel == CHANNEL_SMS
    assert explicitly_not_provided.target == "+14155552671"


@pytest.mark.parametrize(
    ("email", "phone", "expected_channel"),
    [
        ("buyer@example.com", "4155552671", CHANNEL_EMAIL),
        ("buyer@example.com", "", CHANNEL_EMAIL),
        ("", "4155552671", CHANNEL_SMS),
        ("", "", None),
    ],
)
def test_independent_site_contacts_do_not_require_customization_json(
    email: str,
    phone: str,
    expected_channel: str | None,
) -> None:
    contact = _contact(
        platform_order_no="WC12345",
        sales_platform_code="",
        sales_platform_name="WooCommerce",
        email=email,
        email_presence=(
            EMAIL_PRESENCE_PROVIDED if email else EMAIL_PRESENCE_NOT_PROVIDED
        ),
        phone_raw=phone,
        email_source=CONTACT_SOURCE_LINGXING_ORDER_LIST,
        phone_source=CONTACT_SOURCE_WMS,
        verified_phone_e164="",
        phone_verification_state=PHONE_VERIFICATION_MISSING,
    )
    rendered = render_notification(contact, [_package(1)], _config())
    assert rendered.channel == expected_channel
    if expected_channel == CHANNEL_EMAIL:
        assert rendered.sender_email == "cs@billyprint.com"


def test_amazon_country_virtual_email_without_phone_is_a_visible_contact_exception(
    tmp_path,
) -> None:
    store = _ready_database(tmp_path / "amazon-country-virtual.sqlite3", system_count=1)
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(
            email="alias@marketplace.amazon.ca",
            phone_raw="",
            system_order_nos=("10001",),
        )
    )
    store.replace_package_scan(platform, [_package(1)])

    notification = store.prepare_notification(platform, _config())

    assert notification is not None
    assert notification["state"] == NOTIFICATION_MANUAL_EMAIL_REQUIRED
    assert notification["channel"] == CHANNEL_MANUAL_EMAIL
    assert notification["target"] == "alias@marketplace.amazon.ca"
    assert notification["last_error"] == "manual_email_required_virtual_contact"


def test_contact_provenance_is_part_of_review_fingerprint() -> None:
    packages = [_package(1)]
    authoritative = render_notification(_contact(), packages, _config())
    unknown_source = render_notification(
        _contact(email_source="", phone_source="", source=CONTACT_SOURCE_WMS),
        packages,
        _config(),
    )

    assert authoritative.body == unknown_source.body
    assert authoritative.content_hash != unknown_source.content_hash


@pytest.mark.parametrize("missing_count", [1, 3])
def test_available_soon_is_rendered_once_after_complete_packages(missing_count: int) -> None:
    packages = [_package(1), _package(2)] + [
        _package(index, complete=False) for index in range(3, 3 + missing_count)
    ]
    rendered = render_notification(_contact(), packages, _config())
    assert rendered.package_total == 2 + missing_count
    assert rendered.body.count("· Available soon.") == 1
    assert rendered.body.index("Package b:") < rendered.body.index("Available soon.")
    assert "Package 1:" not in rendered.body
    assert "Package 2:" not in rendered.body


def test_customer_visible_packages_are_lettered_contiguously() -> None:
    rendered = render_notification(
        _contact(),
        [_package(1), _package(2), _package(27)],
        _config(),
    )

    assert "· Package a: FedEx TRACK-1" in rendered.body
    assert "· Package b: FedEx TRACK-2" in rendered.body
    assert "· Package c: FedEx TRACK-27" in rendered.body
    assert "Package aa:" not in rendered.body
    assert "Package 1:" not in rendered.body
    assert "Package 2:" not in rendered.body
    assert "Package 27:" not in rendered.body


def test_pending_system_identity_is_part_of_the_review_fingerprint() -> None:
    first = render_notification(
        _contact(system_order_nos=("10001", "20001")),
        [_package(1)],
        _config(),
    )
    second = render_notification(
        _contact(system_order_nos=("10001", "30001")),
        [_package(1)],
        _config(),
    )

    assert first.body == second.body
    assert first.package_missing == second.package_missing == 1
    assert first.content_hash != second.content_hash


def test_product_title_uses_five_words_without_brand_or_trailing_preposition() -> None:
    assert shorten_product_title(
        "  BillyPrint Custom Canopy Tent 10x10 with Logo | Trade Shows | Waterproof  "
    ) == "Custom Canopy Tent 10x10"
    assert shorten_product_title("Main Product｜关键词｜更多关键词") == "Main Product"
    assert shorten_product_title(
        "BillyPrint Custom Canopy Tent 10x10 with Logo, Personalized Pop Up Tent Packages for Trade Shows, Markets, Events"
    ) == "Custom Canopy Tent 10x10"
    assert shorten_product_title("Main Product，关键词，更多关键词") == "Main Product"
    assert shorten_product_title("  Main   Product , More | Other  ") == "Main Product"
    assert shorten_product_title("Single Product") == "Single Product"
    assert shorten_product_title("Custom Full Wall for 10' Tent") == "Custom Full Wall for 10'"
    assert shorten_product_title("Custom Table Cloth with Business Logo") == (
        "Custom Table Cloth with Business"
    )


def test_existing_unsent_draft_is_regenerated_with_new_product_rule(tmp_path) -> None:
    path = tmp_path / "product-rule-refresh.sqlite3"
    store = _ready_database(path, system_count=1)
    platform = "112-1234567-1234567"
    long_title = "BillyPrint Custom Canopy Tent 10x10 with Logo | Trade Shows"
    store.replace_product_scan(
        platform,
        [
            OrderProductSnapshot(
                platform_order_no=platform,
                system_order_no="10001",
                item_key="ITEM-1",
                local_sku="TENT",
                raw_title=long_title,
                display_title="BillyPrint Custom Canopy Tent 10x10 with Logo",
                has_main_image=True,
                source_payload_hash="LONG-TITLE",
            )
        ],
        ("10001",),
    )
    store.upsert_contact(_contact(system_order_nos=("10001",)))
    store.replace_package_scan(platform, [_package(1)])
    original = store.prepare_notification(platform, _config())
    assert original is not None

    assert store.refresh_current_unsent_product_titles(_config()) == 1
    refreshed = store.get_latest_notification(platform)

    assert refreshed is not None
    assert refreshed["id"] != original["id"]
    assert refreshed["product_names"] == ["Custom Canopy Tent 10x10"]
    assert store.get_notification(original["id"])["state"] == "REJECTED"
    assert store.refresh_current_unsent_product_titles(_config()) == 0


def test_product_rule_refresh_does_not_rewrite_sent_history(tmp_path) -> None:
    store, notification = _email_notification(tmp_path, name="sent-product-history.sqlite3")
    claimed = store.approve_and_claim(notification["id"], _config())
    store.finalize_send(
        claimed["id"],
        accepted=True,
        provider_message_id="sent-history",
        provider_status="success",
    )

    assert store.refresh_current_unsent_product_titles(_config()) == 0
    preserved = store.get_notification(notification["id"])
    assert preserved is not None
    assert preserved["state"] == "ACCEPTED"
    assert preserved["provider_message_id"] == "sent-history"


def test_instruction_package_is_hidden_but_supplies_product_title() -> None:
    instruction = OrderProductSnapshot(
        platform_order_no="112-1234567-1234567",
        system_order_no="10001",
        item_key="ITEM-INSTRUCTION",
        local_sku=" Instruction ",
        raw_title="BillyPrint Custom Canopy Tent 10x10 with Logo | Trade Shows",
        display_title="BillyPrint Custom Canopy Tent 10x10 with Logo",
        has_main_image=True,
        is_instruction=True,
    )
    physical = OrderProductSnapshot(
        platform_order_no="112-1234567-1234567",
        system_order_no="10002",
        item_key="ITEM-PHYSICAL",
        local_sku="10X10-FRAME",
    )
    hidden = PackageSnapshot(
        **{
            **_package(1).__dict__,
            "system_order_no": "10001",
            "final_tracking_no": "INSTRUCTION-TRACKING",
            "waybill_no": "INSTRUCTION-TRACKING",
            "customer_visible": False,
            "visibility_reason": "instruction",
        }
    )
    visible = PackageSnapshot(
        **{
            **_package(2).__dict__,
            "system_order_no": "10002",
            "final_tracking_no": "PHYSICAL-TRACKING",
            "waybill_no": "PHYSICAL-TRACKING",
        }
    )
    rendered = render_notification(
        _contact(system_order_nos=("10001", "10002")),
        [hidden, visible],
        _config(),
        products=[instruction, physical],
    )

    assert rendered.product_names == ("BillyPrint Custom Canopy Tent 10x10 with Logo",)
    assert rendered.package_total == 1
    assert rendered.package_complete == 1
    assert "Product: BillyPrint Custom Canopy Tent 10x10 with Logo" in rendered.body
    assert "· Package a: FedEx PHYSICAL-TRACKING" in rendered.body
    assert "INSTRUCTION-TRACKING" not in rendered.body
    assert "Available soon." not in rendered.body
    assert rendered.subject == "Shipment Update - 112-1234567-1234567"


def test_wms_item_detail_hides_only_proven_instruction_packages() -> None:
    base = {
        "order_number": "20001",
        "platform_order_no": "112-1234567-1234567",
        "wo_number": "WO-MIXED",
        "carrier_name": "UPS",
        "tracking_no": "TRACK-MIXED",
    }
    instruction_only = package_from_wms_row(
        {**base, "item_info": [{"local_sku": "Instruction"}]},
        platform_order_no="112-1234567-1234567",
        manual_system_order_nos=frozenset(),
    )
    mixed = package_from_wms_row(
        {
            **base,
            "item_info": [
                {"local_sku": "Instruction"},
                {"local_sku": "10X10-FRAME"},
            ],
        },
        platform_order_no="112-1234567-1234567",
        manual_system_order_nos=frozenset(),
    )
    unknown = package_from_wms_row(
        base,
        platform_order_no="112-1234567-1234567",
        manual_system_order_nos=frozenset(),
    )

    assert instruction_only.customer_visible is False
    assert mixed.customer_visible is True
    assert unknown.customer_visible is True


def test_product_validation_falls_back_to_sku_without_main_image() -> None:
    no_image = _products(1)
    no_image[0] = OrderProductSnapshot(
        **{**no_image[0].__dict__, "has_main_image": False}
    )
    missing = render_notification(
        _contact(), [_package(1)], _config(), products=no_image
    )
    assert missing.product_names == ("Test Product",)
    assert "Product: Test Product" in missing.body
    assert "product_main_image_missing" not in missing.blocked_reasons
    assert "product_sku_missing" not in missing.blocked_reasons


def test_product_validation_blocks_when_neither_title_nor_physical_sku_exists() -> None:
    missing = render_notification(
        _contact(),
        [_package(1)],
        _config(),
        products=[
            OrderProductSnapshot(
                platform_order_no="112-1234567-1234567",
                system_order_no="10001",
                item_key="ITEM-1",
            )
        ],
    )

    assert missing.product_names == ()
    assert "product_sku_missing" in missing.blocked_reasons


def test_product_validation_allows_mixed_instruction_with_physical() -> None:

    mixed = [
        OrderProductSnapshot(
            platform_order_no="112-1234567-1234567",
            system_order_no="10001",
            item_key="A",
            local_sku="Instruction",
            raw_title="Product",
            display_title="Product",
            has_main_image=True,
            is_instruction=True,
        ),
        OrderProductSnapshot(
            platform_order_no="112-1234567-1234567",
            system_order_no="10001",
            item_key="B",
            local_sku="PHYSICAL",
        ),
    ]
    analysis = analyze_order_products(mixed, expected_system_order_nos=("10001",))
    assert "instruction_mixed_with_physical" not in analysis.blocked_reasons
    assert analysis.instruction_system_order_nos == ()


def test_historical_independent_site_orders_are_not_notification_targets(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    _ready_database(path, system_count=1)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE shipment_jobs SET platform_order_no = 'wc39901'")
        conn.commit()

    store = ShipmentNotificationStore(path)
    targets = store.notification_scan_targets(["wc39901"])
    assert targets == []


def test_new_independent_site_erp_completion_is_not_notification_target(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    completed_at = notification_store_module.utc_now()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE shipment_jobs SET platform_order_no = 'WC39902'")
        conn.execute(
            "UPDATE shipment_erp SET outbounded_at = ?, updated_at = ?",
            (completed_at, completed_at),
        )
        conn.commit()

    targets = store.notification_scan_targets(["WC39902"])

    assert targets == []


def test_existing_unsent_wc_draft_is_suppressed_but_sent_history_is_preserved(
    tmp_path,
) -> None:
    unsent_store, unsent = _email_notification(tmp_path, name="wc-unsent.sqlite3")
    with unsent_store.connect() as conn:
        conn.execute(
            "UPDATE shipment_notifications SET platform_order_no = 'wc40001' WHERE id = ?",
            (unsent["id"],),
        )
        conn.commit()
    migrated_unsent = ShipmentNotificationStore(unsent_store.path)
    suppressed = migrated_unsent.get_notification(unsent["id"])
    assert suppressed is not None
    assert suppressed["state"] == NOTIFICATION_SUPPRESSED
    assert suppressed["last_error"] == "independent_site_customer_notification_disabled"
    assert suppressed["reviews"][-1]["action"] == "AUTO_SUPPRESS_INDEPENDENT_SITE"
    reloaded_suppressed = ShipmentNotificationStore(unsent_store.path).get_notification(
        unsent["id"]
    )
    assert reloaded_suppressed is not None
    assert [
        review
        for review in reloaded_suppressed["reviews"]
        if review["action"] == "AUTO_SUPPRESS_INDEPENDENT_SITE"
    ] == [suppressed["reviews"][-1]]

    sent_store, sent = _email_notification(tmp_path, name="wc-sent.sqlite3")
    claimed = sent_store.approve_and_claim(sent["id"], _config())
    sent_store.finalize_send(
        claimed["id"],
        accepted=True,
        provider_message_id="already-sent",
        provider_status="posting",
    )
    with sent_store.connect() as conn:
        conn.execute(
            "UPDATE shipment_notifications SET platform_order_no = 'wc40002' WHERE id = ?",
            (sent["id"],),
        )
        conn.commit()
    migrated_sent = ShipmentNotificationStore(sent_store.path)
    preserved = migrated_sent.get_notification(sent["id"])
    assert preserved is not None
    assert preserved["state"] == "ACCEPTED"
    assert preserved["provider_message_id"] == "already-sent"


def test_wc_sync_never_creates_customer_notification(tmp_path) -> None:
    path = tmp_path / "wc-history.sqlite3"
    store = _ready_database(path, system_count=1)
    platform = "wc39903"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE shipment_jobs SET platform_order_no = ?", (platform,))
        conn.commit()

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        def __init__(self):
            self.package_count = 1

        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no="10001",
                        order_number=platform,
                        payload={
                            "platform_name": "WooCommerce",
                            "buyer_email": "buyer@example.com",
                            "item_info": [
                                {
                                    "global_item_no": "WC-ITEM",
                                    "local_sku": "WC-PRODUCT",
                                    "title": "WC Product",
                                    "data_json": "{}",
                                }
                            ],
                        },
                    )
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": "10001",
                        "platform_order_no": platform,
                        "wo_number": f"WC-WO-{index}",
                        "consignee": "Customer",
                        "carrier_name": "UPS",
                        "waybill_no": f"WC-TRACK-{index}",
                    }
                    for index in range(1, self.package_count + 1)
                ]
            )

    gateway = _Gateway()
    first = asyncio.run(sync_notification_drafts(gateway, store, _config()))
    assert first["baseline_suppressed_count"] == 0
    assert first["new_draft_count"] == 0
    assert store.get_latest_notification(platform) is None

    gateway.package_count = 2
    second = asyncio.run(sync_notification_drafts(gateway, store, _config()))
    notification = store.get_latest_notification(platform)
    assert second["new_draft_count"] == 0
    assert notification is None


def test_notification_compensation_continuously_observes_automation_sources(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    platform = "112-1234567-1234567"

    first = store.notification_scan_targets()
    assert [target["platform_order_no"] for target in first] == [platform]
    store.record_notification_sync_success(
        platform,
        erp_completed_at=first[0]["erp_completed_at"],
    )
    assert [target["platform_order_no"] for target in store.notification_scan_targets()] == [
        platform
    ]

    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE shipment_erp
            SET updated_at = '2026-07-18T00:00:00Z'
            """
        )
    changed = store.notification_scan_targets()
    assert [target["platform_order_no"] for target in changed] == [platform]

    store.record_notification_sync_retry(
        platform,
        erp_completed_at=changed[0]["erp_completed_at"],
        error="temporary API failure",
    )
    assert store.notification_scan_targets() == []
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE shipment_notification_sync_state
            SET next_attempt_at = '2000-01-01T00:00:00Z'
            WHERE platform_order_no = ?
            """,
            (platform,),
        )
    assert [target["platform_order_no"] for target in store.notification_scan_targets()] == [
        platform
    ]

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE shipment_erp SET completion_source = 'MANUAL_DETECTED'"
        )
    assert store.notification_scan_targets() == []


def test_amazon_full_scan_silently_baselines_then_notifies_new_package(tmp_path) -> None:
    path = tmp_path / "full-scan.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    store = ShipmentNotificationStore(path)
    platform = "112-7654321-1234567"

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        def __init__(self):
            self.package_count = 1

        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no="20001",
                        order_number=platform,
                        payload={
                            "platform_code": "10001",
                            "platform_name": "Amazon",
                            "buyer_email": "buyer@example.com",
                            "paid_at": "2026-08-03T00:00:00Z",
                            "item_info": [
                                {
                                    "global_item_no": "ITEM-1",
                                    "local_sku": "PHYSICAL-1",
                                    "title": "Physical Product",
                                    "data_json": "{}",
                                }
                            ],
                        },
                    )
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": "20001",
                        "platform_order_no": platform,
                        "wo_number": f"WO-{index}",
                        "consignee": "Customer",
                        "carrier_name": "UPS",
                        "tracking_no": f"TRACK-{index}",
                    }
                    for index in range(1, self.package_count + 1)
                ]
            )

    gateway = _Gateway()
    backfill = lambda _targets: {
        "_api_fallback_eligible_platforms": [platform]
    }

    first = asyncio.run(
        sync_notification_drafts(
            gateway,
            store,
            _config(),
            contact_backfill=backfill,
            discovery_filter_windows=({},),
        )
    )
    assert first["bootstrap_order_count"] == 1
    assert first["baseline_suppressed_count"] == 1, first
    assert store.get_latest_notification(platform) is None

    gateway.package_count = 2
    second = asyncio.run(
        sync_notification_drafts(
            gateway,
            store,
            _config(),
            contact_backfill=backfill,
            discovery_filter_windows=({},),
        )
    )

    notification = store.get_latest_notification(platform)
    assert second["new_draft_count"] == 1
    assert notification is not None
    assert notification["source_kind"] == "AMAZON_FULL_SCAN"
    assert "TRACK-1" in notification["body"]
    assert "TRACK-2" in notification["body"]


def test_amazon_full_scan_excludes_original_amazon_item_when_image_field_is_missing(
    tmp_path,
) -> None:
    path = tmp_path / "full-scan-ineligible.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    store = ShipmentNotificationStore(path)
    platform = "112-7654321-7654321"

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no="20002",
                        order_number=platform,
                        payload={
                            "platform_code": "10001",
                            "platform_name": "Amazon",
                            "item_info": [
                                {
                                    "global_item_no": "ITEM-2",
                                    "product_no": "B0D1T9P2PR",
                                    "local_sku": "PHYSICAL-2",
                                    "title": "Original Amazon Product",
                                    "data_json": "{}",
                                }
                            ],
                        },
                    )
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            raise AssertionError("ineligible discovery must not query WMS")

    report = asyncio.run(
        sync_notification_drafts(
            _Gateway(),
            store,
            _config(),
            discovery_filter_windows=({},),
        )
    )

    assert report["discovered_order_count"] == 0
    assert report["eligible_order_count"] == 0


def test_amazon_full_scan_keeps_instruction_as_forced_compensation_trigger() -> None:
    platform = "112-7654321-8888888"

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no="20003",
                        order_number=platform,
                        payload={
                            "platform_code": "10001",
                            "platform_name": "Amazon",
                            "paid_at": "2026-08-03T00:00:00Z",
                            "item_info": [
                                {
                                    "global_item_no": "ITEM-INSTRUCTION",
                                    "product_no": "B0F5CKNVYJ",
                                    "local_sku": "Instruction",
                                    "title": "Original Amazon Product",
                                    "data_json": '{"snapshot_image":"main.jpg"}',
                                }
                            ],
                        },
                    )
                ]
            )

    discovered = asyncio.run(
        _discover_recent_amazon_orders(_Gateway(), _config(), ({},))
    )

    assert len(discovered) == 1
    assert discovered[0]["platform_order_no"] == platform
    assert discovered[0]["eligibility_reason"] == "CONTAINS_INSTRUCTION"


def test_amazon_full_scan_includes_manual_fulfillment_item_without_marketplace_id() -> None:
    platform = "112-7654321-9999999"

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no="20004",
                        order_number=platform,
                        payload={
                            "platform_code": "10001",
                            "platform_name": "Amazon",
                            "paid_at": "2026-08-03T00:00:00Z",
                            "item_info": [
                                {
                                    "global_item_no": "ITEM-MANUAL-SPLIT",
                                    "local_sku": "10X10-FRAME",
                                    "title": "",
                                    "data_json": "{}",
                                }
                            ],
                        },
                    )
                ]
            )

    discovered = asyncio.run(
        _discover_recent_amazon_orders(_Gateway(), _config(), ({},))
    )

    assert len(discovered) == 1
    assert discovered[0]["platform_order_no"] == platform
    assert discovered[0]["eligibility_reason"] == "MANUAL_FULFILLMENT_ITEM"


def test_amazon_full_scan_reports_safe_discovery_exception_details(tmp_path) -> None:
    path = tmp_path / "full-scan-discovery-error.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    store = ShipmentNotificationStore(path)

    class _DiscoveryError(RuntimeError):
        request_id = "request-safe-503"
        status_code = 503
        code = "LINGXING_TEMPORARY"
        operation = "list_orders"

    class _Gateway:
        async def list_orders(self, **_kwargs):
            raise _DiscoveryError("secret bearer value must not be persisted")

    report = asyncio.run(
        sync_notification_drafts(
            _Gateway(),
            store,
            _config(),
            discovery_filter_windows=({},),
        )
    )

    assert report["discovery_error_count"] == 1
    assert report["discovery_error_type"] == "_DiscoveryError"
    assert report["discovery_error_request_id"] == "request-safe-503"
    assert report["discovery_error_http_status"] == 503
    assert report["discovery_error_api_code"] == "LINGXING_TEMPORARY"
    assert report["discovery_error_operation"] == "list_orders"
    assert re.fullmatch(r"[0-9a-f]{32}", report["discovery_error_id"])
    serialized = json.dumps(report["discovery_error"], ensure_ascii=False)
    assert "secret bearer value" not in serialized
    assert "<omitted-for-sensitive-data-safety>" in serialized


def test_missing_or_historical_packages_never_leave_customer_letter_gaps() -> None:
    rendered = render_notification(
        _contact(),
        [
            _package(1),
            _package(2),
            _package(3, complete=False),
            _package(4, complete=False),
            _package(5),
            _package(6),
        ],
        _config(),
    )

    assert "· Package a: FedEx TRACK-1" in rendered.body
    assert "· Package b: FedEx TRACK-2" in rendered.body
    assert "· Package c: FedEx TRACK-5" in rendered.body
    assert "· Package d: FedEx TRACK-6" in rendered.body
    assert "Package e:" not in rendered.body
    assert "Package f:" not in rendered.body
    assert rendered.body.count("· Available soon.") == 1


@pytest.mark.parametrize(
    ("carrier", "expected"),
    [
        ("Federal Express", "https://www.fedex.com/fedextrack/?trknbr=TRACK%201%2F2&locale=en_US"),
        ("UPS", "https://www.ups.com/track?loc=en_US&tracknum=TRACK%201%2F2"),
        ("USPS", "https://tools.usps.com/go/TrackConfirmAction?tLabels=TRACK%201%2F2"),
        ("DHL Express", "https://www.dhl.com/global-en/home/tracking.html?tracking-id=TRACK%201%2F2"),
        ("GOFO Express", "https://www.gofoexpress.com/tracking.html?searchID=TRACK%201%2F2"),
        ("Yanwen", "https://track.yw56.com.cn/en/querydel?nums=TRACK%201%2F2"),
        ("SpeedX", "https://tracking.speedx.io/TRACK%201%2F2"),
        ("UniUni", "https://www.uniuni.com/tracking/?no=TRACK%201%2F2"),
        ("1ST", "https://www.17track.net/en/track?nums=TRACK%201%2F2"),
        ("SwiftX", "https://swiftx-express.com/track?trackingNumber=TRACK%201%2F2"),
        ("Wanb Express", "https://tracking.wanbexpress.com/?trackingNumbers=TRACK%201%2F2"),
        ("untrusted.example/path", "https://www.17track.net/en/track?nums=TRACK%201%2F2"),
    ],
)
def test_tracking_urls_use_allowlisted_hosts_and_encoded_numbers(
    carrier: str, expected: str
) -> None:
    assert tracking_url_for(carrier, "TRACK 1/2") == expected
    assert tracking_url_for(carrier, "") == ""


def test_email_html_links_only_the_escaped_tracking_number() -> None:
    package = PackageSnapshot(
        package_key="10001:WO-1",
        platform_order_no="112-1234567-1234567",
        system_order_no="10001",
        shipment_type=PACKAGE_MANUAL,
        carrier_raw="<Carrier>",
        carrier="<Carrier>",
        waybill_no="A&B / 1",
        final_tracking_no="A&B / 1",
        stable_sequence=1,
        stable_label="a",
    )
    rendered = render_notification(
        _contact(recipient_name="Customer <One>"), [package], _config()
    )

    assert rendered.template_version == "shipment-email-v7"
    assert "Customer &lt;One&gt;" in rendered.body_html
    assert "&lt;Carrier&gt;" in rendered.body_html
    assert "<Carrier>" not in rendered.body_html
    assert (
        'href="https://www.17track.net/en/track?nums=A%26B%20%2F%201"'
        in rendered.body_html
    )
    assert ">A&amp;B / 1</a>" in rendered.body_html


def test_sms_uses_package_letters_and_raw_tracking_links() -> None:
    rendered = render_notification(
        _contact(email=""), [_package(1), _package(2, complete=False)], _config()
    )

    assert rendered.template_version == "shipment-sms-v7"
    assert "· Package a: FedEx TRACK-1\n  Track: https://www.fedex.com/" in rendered.body
    assert "Package 1:" not in rendered.body
    assert rendered.body.count("Track: https://") == 1
    assert rendered.body.count("· Available soon.") == 1
    assert rendered.body_html == ""


def test_no_blank_package_omits_available_soon_and_sms_has_metrics() -> None:
    email = render_notification(_contact(), [_package(1), _package(2)], _config())
    assert "Available soon." not in email.body
    sms = render_notification(_contact(email=""), [_package(1)], _config())
    assert sms.sms_encoding == "Unicode"
    assert sms.sms_character_count > 0
    assert sms.sms_segment_count > 1


@pytest.mark.parametrize("placeholder", ["-", "--", "N/A", "null", "未知"])
def test_recipient_name_placeholders_are_never_sendable(placeholder: str) -> None:
    rendered = render_notification(
        _contact(recipient_name=placeholder), [_package(1)], _config()
    )
    assert rendered.recipient_name == ""
    assert "recipient_name_missing" in rendered.blocked_reasons
    assert "Dear -," not in rendered.body


def test_local_queue_membership_selects_the_required_real_tracking_column() -> None:
    manual = package_from_wms_row(
        {
            "order_number": "10001",
            "wo_number": "WO-1",
            "logistics_provider_name": "overseas warehouse",
            "logistics_type_name": "FedEx-阿里巴巴",
            "warehouse_name": "默认仓库",
            "waybill_no": "REAL-WAYBILL",
            "tracking_no": "ALS00000000001",
        },
        platform_order_no="112-1234567-1234567",
        manual_system_order_nos={"10001"},
    )
    assert manual.shipment_type == PACKAGE_MANUAL
    assert manual.final_tracking_no == "REAL-WAYBILL"

    overseas = package_from_wms_row(
        {
            "order_number": "10002",
            "wo_number": "WO-2",
            "logistics_provider_name": "manual-Alibaba Logistics",
            "carrier_name": "UniUni",
            "waybill_no": "internal-value",
            "tracking_no": "UUS123456",
        },
        platform_order_no="112-1234567-1234567",
        manual_system_order_nos={"10001"},
    )
    assert overseas.shipment_type == PACKAGE_OVERSEAS_AUTO
    assert overseas.final_tracking_no == "UUS123456"


@pytest.mark.parametrize(
    ("carrier", "tracking_no", "expected"),
    [
        ("燕文", "UG854485508YP", "Yanwen"),
        ("联邮通服装专线", "4PX3003004509484CN", "4PX"),
        ("手动-Alibaba logistics > FedEx-全程", "874084304695", "FedEx"),
        ("sf-international", "SF123456789", "SF International"),
        ("万邦速达", "WNBAA0486972500YQ", "Wanb Express"),
        ("未知中文承运商", "UNMATCHED123", "International Carrier"),
    ],
)
def test_customer_carrier_names_are_always_english(
    carrier: str,
    tracking_no: str,
    expected: str,
) -> None:
    display = customer_carrier_display_name(carrier, tracking_no)

    assert display == expected
    assert display.isascii()


def test_chinese_wms_carriers_never_reach_email_html_or_sms() -> None:
    package = package_from_wms_row(
        {
            "order_number": "10001",
            "wo_number": "WO-1",
            "carrier_name": "联邮通服装专线",
            "waybill_no": "4PX3003004509484CN",
            "tracking_no": "4PX3003004509484CN",
        },
        platform_order_no="112-1234567-1234567",
        manual_system_order_nos={"10001"},
    )
    email = render_notification(_contact(), [package], _config())
    sms = render_notification(_contact(email=""), [package], _config())

    assert package.carrier == "4PX"
    assert "Package a: 4PX 4PX3003004509484CN" in email.body
    assert "Package a: 4PX " in email.body_html
    assert "Package a: 4PX 4PX3003004509484CN" in sms.body
    assert "联邮通" not in email.body
    assert "联邮通" not in email.body_html
    assert "联邮通" not in sms.body


@pytest.mark.parametrize(
    "row",
    [
        {"status": 4, "status_name": "\u5df2\u622a\u5355"},
        {"status": "4", "status_name": ""},
        {"status": 3, "cancel_status": 1},
        {"status": 3, "status_name": "\u5df2\u53d6\u6d88"},
        {"status": 3, "status_name": "Cancelled"},
    ],
)
def test_terminal_wms_rows_are_excluded_from_customer_notifications(row) -> None:
    assert is_terminal_wms_row(row) is True


def test_active_wms_row_is_not_excluded_from_customer_notifications() -> None:
    assert (
        is_terminal_wms_row(
            {"status": 3, "status_name": "\u5df2\u51fa\u5e93", "cancel_status": 0}
        )
        is False
    )


def test_wms_snapshot_hash_ignores_non_business_response_fields() -> None:
    base = {
        "order_number": "10001",
        "wo_number": "WO-1",
        "logistics_provider_name": "manual-Alibaba Logistics",
        "logistics_type_name": "FedEx",
        "waybill_no": "REAL-WAYBILL",
        "tracking_no": "ALS00000000001",
    }
    first = package_from_wms_row(
        {**base, "request_generated_at": "volatile-1"},
        platform_order_no="112-1234567-1234567",
        manual_system_order_nos={"10001"},
    )
    second = package_from_wms_row(
        {**base, "request_generated_at": "volatile-2"},
        platform_order_no="112-1234567-1234567",
        manual_system_order_nos={"10001"},
    )
    assert first.source_payload_hash == second.source_payload_hash


def test_candidate_discovery_never_persists_notification_contact(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    workflow = ShipmentWorkflowStore(path)
    workflow.upsert_candidate(
        ShipmentCandidate(
            system_order_no="10001",
            platform_order_no="112-1234567-1234567",
            logistics_no="ALS00000000001",
            shipment_tag_name="tag",
            receiver_name="Customer",
            receiver_email="customer@example.com",
            receiver_phone="4155552671",
            sales_platform_code="10001",
            sales_platform_name="Amazon",
        )
    )
    contact = ShipmentNotificationStore(path).get_contact("112-1234567-1234567")
    assert contact is None


def test_customization_json_missing_email_overrides_later_platform_alias(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    notification_store = ShipmentNotificationStore(path)
    notification_store.upsert_contact(
        _contact(
            recipient_name="",
            email="",
            email_presence=EMAIL_PRESENCE_NOT_PROVIDED,
            phone_raw="4155552671",
            sales_platform_code="",
            sales_platform_name="",
            source=CONTACT_SOURCE_CUSTOMIZATION_JSON,
        )
    )
    ShipmentWorkflowStore(path).upsert_candidate(
        ShipmentCandidate(
            system_order_no="10001",
            platform_order_no="112-1234567-1234567",
            logistics_no="ALS00000000001",
            shipment_tag_name="tag",
            receiver_name="Customer",
            receiver_email="alias@marketplace.amazon.com",
            receiver_phone="14155552671",
            sales_platform_code="10001",
            sales_platform_name="Amazon",
        )
    )
    contact = notification_store.get_contact("112-1234567-1234567")
    assert contact is not None
    assert contact.recipient_name == ""
    assert contact.email == ""
    assert contact.email_presence == EMAIL_PRESENCE_NOT_PROVIDED
    assert render_notification(contact, [_package(1)], _config()).channel == CHANNEL_SMS


def test_customization_task_persists_explicit_missing_email_to_queue_database(tmp_path) -> None:
    settings = DesktopSettings()
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=dict,
    )
    changed = runner._persist_customization_notification_contact(
        {
            "platform_order_no": "112-1234567-1234567",
            "system_order_no": "10001",
            "recipient_name": "Customer",
            "phone": "4155552671",
            "email": None,
            "customer_email_provided": False,
            "contact_writeback_recorded": True,
            "contact_writeback_already_done": False,
            "contact_value_source": "customization_json",
        },
        platform_order_no="112-1234567-1234567",
        settings=settings,
    )
    assert changed is True
    contact = ShipmentNotificationStore(tmp_path / settings.queue_path).get_contact(
        "112-1234567-1234567"
    )
    assert contact is not None
    assert contact.email == ""
    assert contact.email_presence == EMAIL_PRESENCE_NOT_PROVIDED
    assert contact.phone_raw == "4155552671"
    assert contact.recipient_name == ""
    assert contact.recipient_name_source == ""


def test_notification_store_initializes_schema_once_per_instance(
    tmp_path,
    monkeypatch,
) -> None:
    calls = 0
    original = notification_store_module.initialize_notification_schema

    def counted_initialize(conn):
        nonlocal calls
        calls += 1
        original(conn)

    monkeypatch.setattr(
        notification_store_module,
        "initialize_notification_schema",
        counted_initialize,
    )
    store = ShipmentNotificationStore(
        tmp_path / "queue.sqlite3",
        timeout_seconds=0.25,
    )

    assert store.get_contact("112-1234567-1234567") is None
    assert store.get_contact("112-1234567-1234567") is None
    with store.connect() as conn:
        busy_timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert calls == 1
    assert busy_timeout_ms == 250


def _ready_database(path, *, system_count: int = 5) -> ShipmentNotificationStore:
    ShipmentWorkflowStore(path).initialize()
    now = "2026-07-17T00:00:00Z"
    with sqlite3.connect(path) as conn:
        for index in range(1, system_count + 1):
            conn.execute(
                """
                INSERT INTO shipment_jobs (
                    logistics_no, system_order_no, platform_order_no,
                    shipment_tag_name, identity_state, first_seen_at,
                    last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'tag', 'ACTIVE', ?, ?, ?, ?)
                """,
                (
                    f"ALS{index:011d}",
                    f"1000{index}",
                    "112-1234567-1234567",
                    now,
                    now,
                    now,
                    now,
                ),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO shipment_erp ("
                "job_id, state, checkpoint, completion_source, updated_at"
                ") VALUES (?, 'DONE', 'OUTBOUNDED', 'AUTOMATION', ?)",
                (job_id, now),
            )
        conn.commit()
    store = ShipmentNotificationStore(path)
    store.replace_product_scan(
        "112-1234567-1234567",
        _products(system_count),
        tuple(f"1000{i}" for i in range(1, system_count + 1)),
    )
    return store


def test_first_automated_erp_package_can_create_partial_notification(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=2)
    platform = "112-1234567-1234567"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE shipment_erp
            SET state = 'WAITING', checkpoint = 'NONE', completion_source = ''
            WHERE job_id = (
                SELECT id FROM shipment_jobs WHERE system_order_no = '10002'
            )
            """
        )
        conn.commit()
    store.upsert_contact(
        _contact(system_order_nos=("10001", "10002"))
    )
    store.replace_package_scan(platform, [_package(1)])

    targets = store.notification_scan_targets()
    notification = store.prepare_notification(platform, _config())

    assert len(targets) == 1
    assert targets[0]["queue_total"] == 2
    assert targets[0]["queue_complete"] == 1
    assert notification is not None
    assert notification["state"] == NOTIFICATION_AWAITING_REVIEW
    assert notification["queue_total"] == 2
    assert notification["queue_complete"] == 1
    assert notification["package_complete"] == 1
    assert notification["package_missing"] == 1
    assert "Available soon." in notification["body"]


class _RecipientNamePage:
    def __init__(self, items):
        self.items = items
        self.total = len(items)


class _RecipientNameGateway:
    def __init__(self, names=("Customer Alpha", "Customer Beta")):
        self.names = tuple(names)

    async def list_orders(self, **_kwargs):
        return _RecipientNamePage(
            [
                SimpleNamespace(
                    global_order_no=f"1000{index}",
                    order_number="112-1234567-1234567",
                    payload={
                        "item_info": [
                            {
                                "global_item_no": f"ITEM-{index}",
                                "platform_order_no": "112-1234567-1234567",
                                "local_sku": f"PRODUCT-{index}",
                                "title": "Test Product" if index == 1 else "",
                                "data_json": (
                                    '{"snapshot_image":"main.jpg"}'
                                    if index == 1
                                    else "{}"
                                ),
                            }
                        ]
                    },
                )
                for index in (1, 2)
            ]
        )

    async def list_wms_orders(self, **_kwargs):
        return _RecipientNamePage(
            [
                {
                    "order_number": f"1000{index}",
                    "platform_order_no": "112-1234567-1234567",
                    "wo_number": f"WO-{index}",
                    "consignee": self.names[index - 1],
                    "carrier_name": "FedEx",
                    "waybill_no": f"TRACK-{index}",
                }
                for index in (1, 2)
            ]
        )


def test_recipient_name_conflict_can_be_selected_for_review_draft(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=2)
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(system_order_nos=("10001", "10002"))
    )
    observed = []

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no=f"1000{index}",
                        order_number=platform,
                        payload={
                            "item_info": [
                                {
                                    "global_item_no": f"ITEM-{index}",
                                    "platform_order_no": platform,
                                    "local_sku": f"PRODUCT-{index}",
                                    "title": "Test Product" if index == 1 else "",
                                    "data_json": (
                                        '{"snapshot_image":"main.jpg"}'
                                        if index == 1
                                        else "{}"
                                    ),
                                }
                            ]
                        },
                    )
                    for index in (1, 2)
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": "10001",
                        "platform_order_no": platform,
                        "wo_number": "WO-1",
                        "consignee": "Customer Alpha",
                        "carrier_name": "FedEx",
                        "waybill_no": "TRACK-1",
                    },
                    {
                        "order_number": "10002",
                        "platform_order_no": platform,
                        "wo_number": "WO-2",
                        "consignee": "Customer Beta",
                        "carrier_name": "FedEx",
                        "waybill_no": "TRACK-2",
                    },
                ]
            )

    async def choose_second(order_no, names):
        observed.append((order_no, names))
        return names[1]

    result = asyncio.run(
        sync_notification_drafts(
            _Gateway(),
            store,
            _config(),
            recipient_name_resolver=choose_second,
        )
    )

    assert observed == [
        (platform, ("Customer Alpha", "Customer Beta"))
    ]
    assert result["recipient_name_conflict_count"] == 1
    assert result["recipient_name_selection_count"] == 1
    assert result["recipient_name_selection_unresolved_count"] == 0
    assert result["failed_order_count"] == 0
    notification = store.get_latest_notification(platform)
    assert notification is not None
    assert notification["state"] == NOTIFICATION_AWAITING_REVIEW
    assert notification["recipient_name"] == "Customer Beta"


def test_policy_mask_plus_one_real_recipient_name_is_selected_without_prompt(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    platform = "112-1234567-1234567"
    store = _ready_database(path, system_count=2)
    store.upsert_contact(_contact(system_order_nos=("10001", "10002")))

    async def prompt_forbidden(_order_no, _names):
        raise AssertionError("a policy mask plus one real name must not prompt")

    result = asyncio.run(
        sync_notification_drafts(
            _RecipientNameGateway(
                ("亚马逊政策要求，暂停显示", "Customer Alpha")
            ),
            store,
            _config(),
            platform_order_nos=(platform,),
            recipient_name_resolver=prompt_forbidden,
        )
    )

    assert result["recipient_name_policy_masked_count"] == 1
    assert result["recipient_name_conflict_count"] == 0
    assert result["recipient_name_selection_prompt_count"] == 0
    notification = store.get_latest_notification(platform)
    assert notification is not None
    assert notification["state"] == NOTIFICATION_AWAITING_REVIEW
    assert notification["recipient_name"] == "Customer Alpha"


def test_recipient_name_choice_is_reused_after_restart_and_unique_scan(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    platform = "112-1234567-1234567"
    store = _ready_database(path, system_count=2)
    store.upsert_contact(_contact(system_order_nos=("10001", "10002")))
    prompts = []

    async def choose_beta(order_no, names):
        prompts.append((order_no, names))
        return "Customer Beta"

    first = asyncio.run(
        sync_notification_drafts(
            _RecipientNameGateway(),
            store,
            _config(),
            platform_order_nos=(platform,),
            recipient_name_resolver=choose_beta,
        )
    )
    assert first["recipient_name_selection_prompt_count"] == 1
    assert first["recipient_name_selection_count"] == 1
    assert first["recipient_name_selection_reused_count"] == 0
    assert prompts == [
        (platform, ("Customer Alpha", "Customer Beta"))
    ]

    # A later partial/unique WMS view may update the current contact, but it
    # must not erase the user's durable conflict decision.
    unique = asyncio.run(
        sync_notification_drafts(
            _RecipientNameGateway(("Customer Alpha", "Customer Alpha")),
            ShipmentNotificationStore(path),
            _config(),
            platform_order_nos=(platform,),
        )
    )
    assert unique["recipient_name_conflict_count"] == 0
    assert ShipmentNotificationStore(path).get_contact(platform).recipient_name == (
        "Customer Alpha"
    )
    assert ShipmentNotificationStore(path).remembered_recipient_name_choice(
        platform,
        ("Customer Alpha", "Customer Gamma"),
    ) == ""

    async def repeated_prompt_forbidden(_order_no, _names):
        raise AssertionError("a remembered recipient name must not prompt again")

    restarted_store = ShipmentNotificationStore(path)
    repeated = asyncio.run(
        sync_notification_drafts(
            _RecipientNameGateway(),
            restarted_store,
            _config(),
            platform_order_nos=(platform,),
            recipient_name_resolver=repeated_prompt_forbidden,
        )
    )
    assert repeated["recipient_name_selection_prompt_count"] == 0
    assert repeated["recipient_name_selection_count"] == 0
    assert repeated["recipient_name_selection_reused_count"] == 1
    assert restarted_store.get_contact(platform).recipient_name == "Customer Beta"

    changed_prompts = []

    async def choose_changed_name(order_no, names):
        changed_prompts.append((order_no, names))
        return "Customer Gamma"

    changed = asyncio.run(
        sync_notification_drafts(
            _RecipientNameGateway(("Customer Alpha", "Customer Gamma")),
            ShipmentNotificationStore(path),
            _config(),
            platform_order_nos=(platform,),
            recipient_name_resolver=choose_changed_name,
        )
    )
    assert changed["recipient_name_selection_prompt_count"] == 1
    assert changed["recipient_name_selection_count"] == 1
    assert changed["recipient_name_selection_reused_count"] == 0
    assert changed_prompts == [
        (platform, ("Customer Alpha", "Customer Gamma"))
    ]


def test_legacy_wms_contact_choice_is_imported_without_prompt(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    platform = "112-1234567-1234567"
    store = _ready_database(path, system_count=2)
    store.upsert_contact(
        _contact(
            recipient_name="Customer Beta",
            system_order_nos=("10001", "10002"),
        )
    )

    async def prompt_forbidden(_order_no, _names):
        raise AssertionError("the pre-upgrade contact choice must be reused")

    result = asyncio.run(
        sync_notification_drafts(
            _RecipientNameGateway(),
            store,
            _config(),
            platform_order_nos=(platform,),
            recipient_name_resolver=prompt_forbidden,
        )
    )

    assert result["recipient_name_selection_prompt_count"] == 0
    assert result["recipient_name_selection_reused_count"] == 1
    with sqlite3.connect(path) as conn:
        choice = conn.execute(
            "SELECT selected_name, selection_source "
            "FROM shipment_notification_recipient_name_choices "
            "WHERE platform_order_no = ?",
            (platform,),
        ).fetchone()
    assert choice == ("Customer Beta", "LEGACY_CONTACT")


def test_legacy_notification_choice_wins_over_later_contact_refresh(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    platform = "112-1234567-1234567"
    store = _ready_database(path, system_count=2)
    store.upsert_contact(_contact(system_order_nos=("10001", "10002")))

    async def choose_beta(_order_no, _names):
        return "Customer Beta"

    first = asyncio.run(
        sync_notification_drafts(
            _RecipientNameGateway(),
            store,
            _config(),
            platform_order_nos=(platform,),
            recipient_name_resolver=choose_beta,
        )
    )
    assert first["recipient_name_selection_count"] == 1
    assert store.get_latest_notification(platform)["recipient_name"] == "Customer Beta"

    # Recreate the exact upgrade boundary: v14 had notification history but no
    # dedicated choice table, and a later partial WMS scan changed the contact.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE shipment_notification_recipient_name_choices")
        conn.execute(
            "UPDATE shipment_order_contacts "
            "SET recipient_name = 'Customer Alpha', recipient_name_source = ? "
            "WHERE platform_order_no = ?",
            (CONTACT_SOURCE_WMS, platform),
        )
        conn.execute("PRAGMA user_version = 14")
        conn.commit()
    ShipmentWorkflowStore(path).initialize()

    async def repeated_prompt_forbidden(_order_no, _names):
        raise AssertionError("notification history must prevent a repeated prompt")

    restarted_store = ShipmentNotificationStore(path)
    repeated = asyncio.run(
        sync_notification_drafts(
            _RecipientNameGateway(),
            restarted_store,
            _config(),
            platform_order_nos=(platform,),
            recipient_name_resolver=repeated_prompt_forbidden,
        )
    )

    assert repeated["recipient_name_selection_prompt_count"] == 0
    assert repeated["recipient_name_selection_reused_count"] == 1
    assert restarted_store.get_contact(platform).recipient_name == "Customer Beta"
    with sqlite3.connect(path) as conn:
        choice = conn.execute(
            "SELECT selected_name, selection_source "
            "FROM shipment_notification_recipient_name_choices "
            "WHERE platform_order_no = ?",
            (platform,),
        ).fetchone()
    assert choice == ("Customer Beta", "LEGACY_NOTIFICATION")


def test_new_recipient_name_conflict_prompts_before_slow_local_updates(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    platform = "112-1234567-1234567"
    store = _ready_database(path, system_count=2)
    store.upsert_contact(_contact(system_order_nos=("10001", "10002")))
    events = []
    original_replace_product_scan = store.replace_product_scan

    def replace_product_scan(*args, **kwargs):
        events.append("product-persistence")
        return original_replace_product_scan(*args, **kwargs)

    store.replace_product_scan = replace_product_scan

    def contact_backfill(_targets):
        events.append("contact-backfill")
        return {"_api_fallback_eligible_platforms": ()}

    async def choose_first(_order_no, names):
        events.append("recipient-popup")
        return names[0]

    result = asyncio.run(
        sync_notification_drafts(
            _RecipientNameGateway(),
            store,
            _config(),
            platform_order_nos=(platform,),
            recipient_name_resolver=choose_first,
            contact_backfill=contact_backfill,
        )
    )

    assert result["recipient_name_selection_prompt_count"] == 1
    assert events == [
        "recipient-popup",
        "contact-backfill",
        "product-persistence",
    ]


def test_unresolved_recipient_name_conflict_creates_visible_retry_alert(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=2)
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(system_order_nos=("10001", "10002"))
    )

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no=f"1000{index}",
                        order_number=platform,
                        payload={
                            "item_info": [
                                {
                                    "global_item_no": f"ITEM-{index}",
                                    "platform_order_no": platform,
                                    "local_sku": f"PRODUCT-{index}",
                                    "title": "Test Product" if index == 1 else "",
                                    "data_json": (
                                        '{"snapshot_image":"main.jpg"}'
                                        if index == 1
                                        else "{}"
                                    ),
                                }
                            ]
                        },
                    )
                    for index in (1, 2)
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": "10001",
                        "platform_order_no": platform,
                        "wo_number": "WO-1",
                        "consignee": "Customer Alpha",
                        "carrier_name": "FedEx",
                        "waybill_no": "TRACK-1",
                    },
                    {
                        "order_number": "10002",
                        "platform_order_no": platform,
                        "wo_number": "WO-2",
                        "consignee": "Customer Beta",
                        "carrier_name": "FedEx",
                        "waybill_no": "TRACK-2",
                    },
                ]
            )

    async def leave_unresolved(_order_no, _names):
        return None

    result = asyncio.run(
        sync_notification_drafts(
            _Gateway(),
            store,
            _config(),
            recipient_name_resolver=leave_unresolved,
        )
    )

    assert result["recipient_name_conflict_count"] == 1
    assert result["recipient_name_selection_unresolved_count"] == 1
    assert result["recipient_name_retry_alert_count"] == 1
    assert result["failed_order_count"] == 1
    contact = store.get_contact(platform)
    assert contact is not None
    assert contact.recipient_name == ""
    notification = store.get_latest_notification(platform)
    assert notification is not None
    assert notification["state"] == NOTIFICATION_BLOCKED
    assert notification["last_error"] == "recipient_name_conflict_unresolved"
    with sqlite3.connect(path) as conn:
        sync_state = conn.execute(
            """
            SELECT state, attempt_count, last_error, next_attempt_at
            FROM shipment_notification_sync_state
            WHERE platform_order_no = ?
            """,
            (platform,),
        ).fetchone()
    assert sync_state is not None
    assert sync_state[0] == "RETRYABLE"
    assert sync_state[1] == 1
    assert "recipient name conflict" in sync_state[2]
    assert sync_state[3]


def test_store_creates_review_snapshot_and_invalidates_stale_approval(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    store.upsert_contact(_contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6))))
    packages = [_package(1), _package(2)] + [
        _package(index, complete=False) for index in range(3, 6)
    ]
    store.replace_package_scan("112-1234567-1234567", packages)
    notification = store.prepare_notification("112-1234567-1234567", _config())
    assert notification is not None
    assert notification["state"] == NOTIFICATION_AWAITING_REVIEW
    assert notification["package_total"] == 5
    assert len(notification["items"]) == 5
    assert notification["body"].count("Available soon.") == 1
    assert "<a href=" in notification["body_html"]
    assert notification["items"][0]["tracking_url"].startswith(
        "https://www.fedex.com/"
    )
    assert notification["items"][2]["tracking_url"] == ""
    assert store.prepare_notification("112-1234567-1234567", _config())["id"] == notification["id"]

    store.upsert_contact(_contact(email="changed@example.com"))
    with pytest.raises(StaleNotificationError):
        store.approve_and_claim(notification["id"], _config())
    latest = store.list_notifications()[0]
    assert latest["revision"] == 2
    assert latest["state"] == NOTIFICATION_AWAITING_REVIEW


def test_manual_contact_edit_reopens_review_and_survives_automatic_scans(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(
            email="",
            email_presence=EMAIL_PRESENCE_NOT_PROVIDED,
            phone_raw="",
            email_source=CONTACT_SOURCE_CUSTOMIZATION_JSON,
            phone_source=CONTACT_SOURCE_CUSTOMIZATION_JSON,
            verified_phone_e164="",
            phone_verification_state=PHONE_VERIFICATION_MISSING,
        )
    )
    store.replace_package_scan(platform, [_package(1)])
    waiting = store.prepare_notification(platform, _config())
    assert waiting is not None
    assert waiting["state"] == NOTIFICATION_WAITING_CONTACT

    updated = store.edit_contact_and_prepare(
        platform,
        email="manual.customer@example.com",
        phone="",
        configuration=_config(),
    )

    assert updated is not None
    assert updated["state"] == NOTIFICATION_AWAITING_REVIEW
    assert updated["recipient_email"] == "manual.customer@example.com"
    contact = store.get_contact(platform)
    assert contact is not None
    assert contact.email_source == CONTACT_SOURCE_DESKTOP_MANUAL
    assert contact.phone_source == CONTACT_SOURCE_DESKTOP_MANUAL

    store.upsert_lingxing_api_contact(
        platform,
        email="api@example.com",
        phone="+14155550000",
    )
    contact = store.get_contact(platform)
    assert contact is not None
    assert contact.email == "manual.customer@example.com"
    assert contact.phone_raw == ""
    assert contact.email_source == CONTACT_SOURCE_DESKTOP_MANUAL
    assert contact.phone_source == CONTACT_SOURCE_DESKTOP_MANUAL

    store.upsert_customization_contact(
        platform,
        email="automatic@example.com",
        phone="4155552671",
    )
    contact = store.get_contact(platform)
    assert contact is not None
    assert contact.email == "manual.customer@example.com"
    assert contact.phone_raw == ""
    assert contact.email_source == CONTACT_SOURCE_DESKTOP_MANUAL
    assert contact.phone_source == CONTACT_SOURCE_DESKTOP_MANUAL


@pytest.mark.parametrize(
    ("email", "phone", "message"),
    [
        ("invalid", "", "邮箱格式无效"),
        ("", "123", "电话格式无效"),
        ("", "", "不能同时为空"),
    ],
)
def test_manual_contact_edit_validates_input(
    tmp_path,
    email: str,
    phone: str,
    message: str,
) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    with pytest.raises(NotificationStateError, match=message):
        store.edit_contact_and_prepare(
            "112-1234567-1234567",
            email=email,
            phone=phone,
            configuration=_config(),
        )


def test_provenance_only_change_does_not_invalidate_identical_review(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    store.upsert_contact(_contact())
    store.replace_package_scan("112-1234567-1234567", [_package(1)])
    notification = store.prepare_notification("112-1234567-1234567", _config())
    assert notification is not None

    # Source metadata participates in the audit fingerprint, but the recipient,
    # provider request and every customer-visible field remain identical.
    store.upsert_contact(_contact(source=CONTACT_SOURCE_WMS))
    claimed = store.approve_and_claim(notification["id"], _config())

    assert claimed["state"] == "SENDING"
    assert claimed["id"] == notification["id"]


def test_manual_completion_suppresses_same_packages_but_allows_a_new_package(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    platform_order_no = "112-1234567-1234567"
    store.upsert_contact(_contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6))))
    store.replace_package_scan(platform_order_no, [_package(index) for index in range(1, 6)])
    notification = store.prepare_notification(platform_order_no, _config())
    assert notification is not None

    result = store.mark_manually_completed(
        [notification["id"]],
        note="Historical ERP shipment notification was sent manually",
    )

    assert result == {"completed": 1}
    completed = store.get_notification(notification["id"])
    assert completed is not None
    assert completed["state"] == NOTIFICATION_MANUALLY_COMPLETED
    assert completed["provider_status"] == "MANUAL_COMPLETION"
    assert completed["provider_message_id"] is None
    assert completed["sent_at"] is None
    assert completed["reviews"][-1]["action"] == "MANUAL_COMPLETION"

    store.replace_package_scan(
        platform_order_no,
        [_package(index) for index in range(1, 5)] + [_package(5, complete=False)],
    )
    after_change = store.prepare_notification(platform_order_no, _config())
    assert after_change is not None
    assert after_change["id"] == notification["id"]
    assert after_change["state"] == NOTIFICATION_MANUALLY_COMPLETED
    assert len(store.list_notifications(latest_only=False)) == 1

    store.replace_package_scan(
        platform_order_no,
        [_package(index) for index in range(1, 7)],
    )
    supplement = store.prepare_notification(platform_order_no, _config())
    assert supplement is not None
    assert supplement["id"] != notification["id"]
    assert supplement["is_supplemental_revision"] is True
    assert "TRACK-6" in supplement["body"]


def test_manual_completion_allows_accepted_notification_and_preserves_evidence(
    tmp_path,
) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    platform = "112-1234567-1234567"
    store.upsert_contact(_contact())
    store.replace_package_scan(platform, [_package(1)])
    notification = store.prepare_notification(platform, _config())
    assert notification is not None
    store.approve_and_claim(notification["id"], _config())
    store.finalize_send(
        notification["id"],
        accepted=True,
        provider_message_id="provider-evidence-1",
        provider_status="ACCEPTED",
    )

    result = store.mark_manually_completed(
        [notification["id"]],
        note="operator verified the customer notification manually",
    )

    assert result == {"completed": 1}
    completed = store.get_notification(notification["id"])
    assert completed is not None
    assert completed["state"] == NOTIFICATION_MANUALLY_COMPLETED
    assert completed["provider_status"] == "ACCEPTED"
    assert completed["provider_message_id"] == "provider-evidence-1"
    assert completed["sent_at"]
    assert completed["reviews"][-1]["action"] == "MANUAL_COMPLETION"


def test_manual_reopen_creates_new_review_revision_and_preserves_history(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6)))
    )
    store.replace_package_scan(platform, [_package(index) for index in range(1, 6)])
    original = store.prepare_notification(platform, _config())
    assert original is not None
    store.mark_manually_completed([original["id"]], note="manual close")

    reopened = store.reopen_for_review(
        original["id"],
        _config(),
        actor="reviewer",
        note="customer requested a new notice",
    )

    assert reopened["id"] != original["id"]
    assert reopened["revision"] == original["revision"] + 1
    assert reopened["state"] == NOTIFICATION_AWAITING_REVIEW
    assert store.get_notification(original["id"])["state"] == NOTIFICATION_MANUALLY_COMPLETED
    assert reopened["reviews"][-1]["action"] == "MANUAL_REOPEN"
    assert reopened["reviews"][-1]["actor"] == "reviewer"
    assert reopened["reviews"][-1]["note"] == "customer requested a new notice"

    with pytest.raises(NotificationStateError):
        store.reopen_for_review(
            reopened["id"],
            _config(),
            actor="reviewer",
            note="cannot reopen a pending review",
        )


def test_cancelled_notification_is_audited_and_not_recreated_by_scan(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    platform = "112-1234567-1234567"
    store.upsert_contact(_contact())
    store.replace_package_scan(platform, [_package(1)])
    notification = store.prepare_notification(platform, _config())
    assert notification is not None

    result = store.cancel_notifications(
        [notification["id"]],
        actor="desktop_user",
        note="客户要求取消通知",
    )

    assert result == {"cancelled": 1}
    cancelled = store.get_notification(notification["id"])
    assert cancelled["state"] == NOTIFICATION_CANCELLED
    assert cancelled["reviews"][-1]["action"] == "CANCEL"
    store.replace_package_scan(platform, [_package(1), _package(2)])
    after_scan = store.prepare_notification(platform, _config())
    assert after_scan["id"] == notification["id"]
    assert after_scan["state"] == NOTIFICATION_CANCELLED

    reopened = store.reopen_for_review(
        notification["id"],
        _config(),
        actor="desktop_user",
        note="恢复客户通知",
    )
    assert reopened["state"] == NOTIFICATION_AWAITING_REVIEW


def _duplicate_notification_for_batch(
    store: ShipmentNotificationStore,
    notification_id: int,
) -> int:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM shipment_notifications WHERE id = ?", (notification_id,)
        ).fetchone()
        assert row is not None
        columns = [name for name in row.keys() if name != "id"]
        values = [row[name] for name in columns]
        values[columns.index("platform_order_no")] = "113-7654321-7654321"
        values[columns.index("idempotency_key")] = "batch-second-idempotency"
        placeholders = ",".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO shipment_notifications ({','.join(columns)}) VALUES ({placeholders})",
            values,
        )
        duplicate_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    return duplicate_id


def test_batch_approval_prevalidates_all_then_continues_after_single_failure(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6)))
    )
    store.replace_package_scan(platform, [_package(index) for index in range(1, 6)])
    first = store.prepare_notification(platform, _config())
    assert first is not None
    second_id = _duplicate_notification_for_batch(store, first["id"])
    calls: list[int] = []

    def send(notification_id: int, *, retry: bool) -> ControlResult:
        assert retry is False
        calls.append(notification_id)
        return ControlResult(
            notification_id == second_id,
            "accepted" if notification_id == second_id else "failed",
        )

    fake = SimpleNamespace(
        _shipment_notification_context=lambda: (store, _config()),
        _send_shipment_notification=send,
        _append_log=lambda *_args, **_kwargs: None,
    )
    blocked = PersistentBackgroundTaskController.approve_shipment_notifications(
        fake, [first["id"], 999999]
    )
    assert blocked.accepted is False
    assert calls == []

    result = PersistentBackgroundTaskController.approve_shipment_notifications(
        fake, [first["id"], second_id]
    )
    assert result.accepted is True
    assert calls == [first["id"], second_id]
    assert result.details["accepted"] == 1
    assert result.details["failed"] == 1


def test_v9_exclusion_reset_is_idempotent_and_prevents_regeneration(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    platform_order_no = "112-1234567-1234567"
    store.upsert_contact(_contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6))))
    store.replace_package_scan(platform_order_no, [_package(index) for index in range(1, 6)])
    notification = store.prepare_notification(platform_order_no, _config())
    assert notification is not None
    assert notification["erp_completed_at"] == "2026-07-17T00:00:00Z"
    assert notification["state_changed_at"] == notification["erp_completed_at"]

    first = store.exclude_and_delete_platforms(
        [platform_order_no], reason="historical notifications already sent manually"
    )
    second = store.exclude_and_delete_platforms(
        [platform_order_no], reason="historical notifications already sent manually"
    )

    assert first == {
        "excluded": 1,
        "notifications_deleted": 1,
        "contacts_deleted": 1,
        "packages_deleted": 5,
    }
    assert second == {
        "excluded": 0,
        "notifications_deleted": 0,
        "contacts_deleted": 0,
        "packages_deleted": 0,
    }
    assert store.notification_scan_targets() == []
    assert store.list_notifications() == []


def test_notification_sync_uses_order_email_and_wms_phone_when_json_is_missing(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE shipment_jobs SET receiver_email = ? WHERE system_order_no = '10001'",
            ("candidate@example.com",),
        )
        conn.commit()

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **kwargs):
            assert kwargs["filters"] == {
                "platform_order_nos": ["112-1234567-1234567"]
            }
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no=f"1000{index}",
                        order_number=None,
                        payload={
                            "buyer_email": "buyer@example.com",
                            "platform_info": [
                                {"platform_order_no": "112-1234567-1234567"}
                            ],
                            "item_info": [
                                {
                                    "global_item_no": f"ITEM-{index}",
                                    "platform_order_no": "112-1234567-1234567",
                                    "local_sku": f"PRODUCT-{index}",
                                    "title": "Test Product" if index == 1 else "",
                                    "data_json": (
                                        '{"snapshot_image":{"cos_id":"image-1"}}'
                                        if index == 1
                                        else "{}"
                                    ),
                                }
                            ],
                        },
                    )
                    for index in range(1, 6)
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": f"1000{index}",
                        "platform_order_no": "112-1234567-1234567",
                        "wo_number": f"WO-{index}",
                        "consignee": "Customer",
                        "receiver_email": "must-not-enter@example.com",
                        "consignee_phone": "4155552671",
                        "platform_name": "Amazon",
                        "logistics_provider_name": "手动-Alibaba Logistics",
                        "logistics_type_name": "FedEx",
                        "waybill_no": f"TRACK-{index}",
                        "tracking_no": f"TRACK-{index}",
                    }
                    for index in range(1, 6)
                ]
            )

    result = asyncio.run(sync_notification_drafts(_Gateway(), store, _config()))

    assert result["notification_count"] == 1
    assert result["contact_update_count"] == 2
    contact = store.get_contact("112-1234567-1234567")
    assert contact is not None
    assert contact.recipient_name == "Customer"
    assert contact.recipient_name_source == CONTACT_SOURCE_WMS
    assert contact.email == "buyer@example.com"
    assert contact.phone_raw == "+14155552671"
    assert contact.email_source == CONTACT_SOURCE_LINGXING_ORDER_LIST
    assert contact.phone_source == CONTACT_SOURCE_WMS
    notification = store.list_notifications()[0]
    assert notification["recipient_name"] == "Customer"
    assert notification["recipient_email"] == "buyer@example.com"
    assert notification["recipient_phone"] == "+14155552671"
    assert notification["state"] == NOTIFICATION_AWAITING_REVIEW
    assert notification["channel"] == CHANNEL_EMAIL


def test_notification_sync_email_only_fallback_enters_mail_review(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    platform = "112-1234567-1234567"

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no="10001",
                        order_number=platform,
                        payload={
                            "buyer_email": "email-only@example.com",
                            "platform_name": "Amazon",
                            "item_info": [
                                {
                                    "global_item_no": "ITEM-1",
                                    "platform_order_no": platform,
                                    "local_sku": "PRODUCT-1",
                                    "title": "Test Product",
                                    "data_json": '{"snapshot_image":"main.jpg"}',
                                }
                            ],
                        },
                    )
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": "10001",
                        "platform_order_no": platform,
                        "wo_number": "WO-1",
                        "consignee": "Customer",
                        "consignee_phone": "-",
                        "carrier_name": "UPS",
                        "waybill_no": "1Z9999999999999999",
                        "tracking_no": "ALS-1",
                    }
                ]
            )

    result = asyncio.run(sync_notification_drafts(_Gateway(), store, _config()))

    assert result["failed_order_count"] == 0
    contact = store.get_contact(platform)
    assert contact is not None
    assert contact.email == "email-only@example.com"
    assert contact.phone_raw == ""
    assert contact.email_source == CONTACT_SOURCE_LINGXING_ORDER_LIST
    notification = store.get_latest_notification(platform)
    assert notification is not None
    assert notification["state"] == NOTIFICATION_AWAITING_REVIEW
    assert notification["channel"] == CHANNEL_EMAIL
    assert notification["target"] == "email-only@example.com"


def test_notification_sync_api_fallback_never_overwrites_json_fields(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(
            email="",
            email_presence=EMAIL_PRESENCE_NOT_PROVIDED,
            phone_raw="4155552671",
            system_order_nos=("10001",),
        )
    )

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no="10001",
                        order_number=platform,
                        payload={
                            "buyer_email": "must-not-overwrite@example.com",
                            "item_info": [
                                {
                                    "global_item_no": "ITEM-1",
                                    "platform_order_no": platform,
                                    "local_sku": "PRODUCT-1",
                                    "title": "Test Product",
                                    "data_json": '{"snapshot_image":"main.jpg"}',
                                }
                            ],
                        },
                    )
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": "10001",
                        "platform_order_no": platform,
                        "wo_number": "WO-1",
                        "consignee": "Customer",
                        "consignee_phone": "4155559999",
                        "carrier_name": "UPS",
                        "waybill_no": "1Z9999999999999999",
                        "tracking_no": "ALS-1",
                    }
                ]
            )

    asyncio.run(sync_notification_drafts(_Gateway(), store, _config()))

    contact = store.get_contact(platform)
    assert contact is not None
    assert contact.email == "must-not-overwrite@example.com"
    assert contact.phone_raw == "4155552671"
    assert contact.email_source == CONTACT_SOURCE_LINGXING_ORDER_LIST
    assert contact.phone_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
    notification = store.get_latest_notification(platform)
    assert notification is not None
    assert notification["state"] == NOTIFICATION_AWAITING_REVIEW
    assert notification["channel"] == CHANNEL_EMAIL


def test_partial_wms_scan_preserves_recipient_name_and_replaces_expected_systems(
    tmp_path,
) -> None:
    store = ShipmentNotificationStore(tmp_path / "queue.sqlite3")
    platform = "112-1234567-1234567"
    store.upsert_wms_recipient_name(
        platform,
        "Customer",
        system_order_nos=("10001", "OLD-SYSTEM"),
    )

    store.upsert_wms_recipient_name(
        platform,
        "",
        system_order_nos=("10001", "20001"),
    )

    contact = store.get_contact(platform)
    assert contact is not None
    assert contact.recipient_name == "Customer"
    assert contact.recipient_name_source == CONTACT_SOURCE_WMS
    assert contact.system_order_nos == ("10001", "20001")


def test_notification_sync_expands_platform_siblings_and_uses_membership_columns(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    store.upsert_contact(_contact(system_order_nos=("10001",)))

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **kwargs):
            assert kwargs["filters"] == {
                "platform_order_nos": ["112-1234567-1234567"]
            }
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no=system_order_no,
                        order_number=None,
                        payload={
                            "item_info": [
                                {"platform_order_no": "112-1234567-1234567"}
                            ]
                        },
                    )
                    for system_order_no in ("10001", "20001")
                ]
            )

        async def list_wms_orders(self, **kwargs):
            assert set(kwargs["filters"]["order_number_arr"]) == {"10001", "20001"}
            return _Page(
                [
                    {
                        "order_number": "10001",
                        "platform_order_no": "112-1234567-1234567",
                        "wo_number": "WO-MANUAL",
                        "consignee": "Customer",
                        "logistics_provider_name": "overseas warehouse",
                        "logistics_type_name": "FedEx",
                        "waybill_no": "MANUAL-WAYBILL",
                        "tracking_no": "MUST-NOT-BE-USED",
                    },
                    {
                        "order_number": "20001",
                        "platform_order_no": "112-1234567-1234567",
                        "wo_number": "WO-OVERSEAS-1",
                        "consignee": "Customer",
                        "logistics_provider_name": "manual-Alibaba Logistics",
                        "carrier_name": "UniUni",
                        "waybill_no": "MUST-NOT-BE-USED-1",
                        "tracking_no": "OVERSEAS-TRACKING-1",
                    },
                    {
                        "order_number": "20001",
                        "platform_order_no": "112-1234567-1234567",
                        "wo_number": "WO-OVERSEAS-2",
                        "consignee": "Customer",
                        "logistics_provider_name": "manual-Alibaba Logistics",
                        "carrier_name": "UniUni",
                        "waybill_no": "MUST-NOT-BE-USED-2",
                        "tracking_no": "OVERSEAS-TRACKING-2",
                    },
                ]
            )

    result = asyncio.run(sync_notification_drafts(_Gateway(), store, _config()))

    assert result["package_update_count"] == 1
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT system_order_no, shipment_type, waybill_no, tracking_no,
                   final_tracking_no
            FROM shipment_package_snapshots
            WHERE platform_order_no = ? AND active = 1
            ORDER BY stable_sequence
            """,
            ("112-1234567-1234567",),
        ).fetchall()
    assert rows == [
        (
            "10001",
            PACKAGE_MANUAL,
            "MANUAL-WAYBILL",
            "MUST-NOT-BE-USED",
            "MANUAL-WAYBILL",
        ),
        (
            "20001",
            PACKAGE_OVERSEAS_AUTO,
            "MUST-NOT-BE-USED-1",
            "OVERSEAS-TRACKING-1",
            "OVERSEAS-TRACKING-1",
        ),
        (
            "20001",
            PACKAGE_OVERSEAS_AUTO,
            "MUST-NOT-BE-USED-2",
            "OVERSEAS-TRACKING-2",
            "OVERSEAS-TRACKING-2",
        ),
    ]
    notification = store.list_notifications()[0]
    assert notification["package_total"] == 3
    assert notification["package_complete"] == 3


def test_notification_sync_uses_main_image_title_and_filters_instruction_package(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    platform = "112-1234567-1234567"
    store.upsert_contact(_contact(system_order_nos=("10001",)))

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    product_rows = {
        "20001": {
            "global_item_no": "ITEM-INSTRUCTION",
            "platform_order_no": platform,
            "local_sku": "Instruction",
            "title": (
                "BillyPrint Custom Canopy Tent 10x10 with Logo | "
                "Pop Up Vendor Tent for Trade Shows"
            ),
            "data_json": '{"snapshot_image":{"cos_id":"image-1","name":"main.jpg"}}',
        },
        "20002": {
            "global_item_no": "ITEM-FRAME",
            "platform_order_no": platform,
            "local_sku": "10X10-FRAME",
            "title": "",
            "data_json": '{"amountRate":"1.0000"}',
        },
        "10001": {
            "global_item_no": "ITEM-TOP",
            "platform_order_no": platform,
            "local_sku": "10X10-TOP",
            "title": "",
            "data_json": '{"amountRate":"1.0000"}',
        },
    }

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no=system_order_no,
                        order_number=platform,
                        payload={"item_info": [product_rows[system_order_no]]},
                    )
                    for system_order_no in ("20001", "20002", "10001")
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": system_order_no,
                        "platform_order_no": platform,
                        "wo_number": f"WO-{system_order_no}",
                        "consignee": "Customer",
                        "carrier_name": "FedEx",
                        "logistics_type_name": "FedEx",
                        "waybill_no": f"WAYBILL-{system_order_no}",
                        "tracking_no": f"TRACK-{system_order_no}",
                    }
                    for system_order_no in ("20001", "20002", "10001")
                ]
            )

    result = asyncio.run(sync_notification_drafts(_Gateway(), store, _config()))

    assert result["product_update_count"] == 1
    notification = store.get_latest_notification(platform)
    assert notification is not None
    assert notification["state"] == NOTIFICATION_AWAITING_REVIEW
    assert notification["product_names"] == ["Custom Canopy Tent 10x10"]
    assert notification["package_total"] == 2
    assert notification["package_complete"] == 2
    assert len(notification["items"]) == 3
    hidden = next(
        item for item in notification["items"] if item["system_order_no"] == "20001"
    )
    assert hidden["customer_visible"] == 0
    assert hidden["visibility_reason"] == "instruction"
    assert hidden["display_label"] == ""
    assert "TRACK-20001" not in notification["body"]
    assert "· Package a: FedEx TRACK-20002" in notification["body"]
    assert "· Package b: FedEx WAYBILL-10001" in notification["body"]


def test_notification_sync_treats_an_omitted_wms_sibling_as_pending_logistics(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    store.replace_package_scan("112-1234567-1234567", [_package(1)])

    class _Page:
        def __init__(self, items, total=None):
            self.items = items
            self.total = len(items) if total is None else total

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no=value,
                        order_number="112-1234567-1234567",
                        payload={},
                    )
                    for value in ("10001", "20001")
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": "10001",
                        "platform_order_no": "112-1234567-1234567",
                        "wo_number": "WO-1",
                        "logistics_type_name": "FedEx",
                        "waybill_no": "TRACK-1",
                    }
                ]
            )

    result = asyncio.run(sync_notification_drafts(_Gateway(), store, _config()))

    assert result["failed_order_count"] == 0
    assert result["partial_logistics_order_count"] == 1
    assert result["missing_system_order_count"] == 1
    assert result["new_draft_count"] == 1

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT package_key, final_tracking_no
            FROM shipment_package_snapshots
            WHERE platform_order_no = ? AND active = 1
            """,
            ("112-1234567-1234567",),
        ).fetchall()
    assert rows == [("10001:WO-1", "TRACK-1")]
    notification = store.list_notifications()[0]
    assert notification["package_total"] == 2
    assert notification["package_complete"] == 1
    assert notification["package_missing"] == 1
    assert notification["body"].count("Available soon.") == 1
    assert len(notification["items"]) == 2
    pending = next(
        item
        for item in notification["items"]
        if item["visibility_reason"] == "pending_wms"
    )
    assert pending["package_snapshot_id"] is None
    assert pending["system_order_no"] == "20001"
    assert pending["display_label"] == ""
    assert pending["is_complete"] == 0


def test_terminal_only_wms_response_deactivates_previous_package_snapshot(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    store.replace_package_scan("112-1234567-1234567", [_package(1)])

    report = store.merge_package_scan(
        "112-1234567-1234567",
        [],
        ["10001"],
        authoritative_observed_system_order_nos=["10001"],
    )

    assert report["changed"] == 1
    assert report["package_complete"] == 0
    assert report["package_missing"] == 1
    with sqlite3.connect(path) as conn:
        active = conn.execute(
            """
            SELECT COUNT(*)
            FROM shipment_package_snapshots
            WHERE platform_order_no = ? AND active = 1
            """,
            ("112-1234567-1234567",),
        ).fetchone()[0]
    assert active == 0


def test_notification_sync_removes_cut_off_4px_ghost_and_reopens_review(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    platform = "112-1234567-1234567"
    store = _ready_database(path, system_count=1)
    store.upsert_contact(_contact())
    valid = PackageSnapshot(
        package_key="10001:WO-VALID",
        platform_order_no=platform,
        system_order_no="10001",
        shipment_type=PACKAGE_MANUAL,
        carrier_raw="Yanwen",
        carrier="Yanwen",
        waybill_no="420306339235990416420600910963",
        tracking_no="",
        final_tracking_no="420306339235990416420600910963",
    )
    ghost = PackageSnapshot(
        package_key="10001:WO-STOPPED",
        platform_order_no=platform,
        system_order_no="10001",
        shipment_type=PACKAGE_MANUAL,
        carrier_raw="4PX",
        carrier="4PX",
        waybill_no="4PX3003004509484CN",
        tracking_no="",
        final_tracking_no="4PX3003004509484CN",
    )
    store.replace_package_scan(platform, [valid, ghost])
    original = store.prepare_notification(platform, _config())
    assert original is not None
    assert original["package_total"] == 2

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no="10001",
                        order_number=platform,
                        payload={
                            "item_info": [
                                {
                                    "global_item_no": "ITEM-1",
                                    "local_sku": "PRODUCT-1",
                                    "title": "Test Product | Keywords",
                                    "data_json": '{"amountRate":"1.0000"}',
                                }
                            ]
                        },
                    )
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": "10001",
                        "platform_order_no": platform,
                        "wo_number": "WO-VALID",
                        "carrier_name": "\u71d5\u6587",
                        "waybill_no": "420306339235990416420600910963",
                        "tracking_no": "",
                        "status": 3,
                        "status_name": "\u5df2\u51fa\u5e93",
                        "cancel_status": 0,
                    },
                    {
                        "order_number": "10001",
                        "platform_order_no": platform,
                        "wo_number": "WO-STOPPED",
                        "carrier_name": "\u8054\u90ae\u901a\u670d\u88c5\u4e13\u7ebf",
                        "waybill_no": "4PX3003004509484CN",
                        "tracking_no": "",
                        "status": 4,
                        "status_name": "\u5df2\u622a\u5355",
                        "cancel_status": 0,
                    },
                ]
            )

    result = asyncio.run(sync_notification_drafts(_Gateway(), store, _config()))

    assert result["failed_order_count"] == 0
    assert result["wms_terminal_row_excluded_count"] == 1
    assert result["new_draft_count"] == 1
    latest = store.get_latest_notification(platform)
    assert latest is not None
    assert latest["id"] != original["id"]
    assert latest["revision"] == original["revision"] + 1
    assert latest["state"] == NOTIFICATION_AWAITING_REVIEW
    assert latest["package_total"] == 1
    assert "4PX3003004509484CN" not in latest["body"]
    assert "420306339235990416420600910963" in latest["body"]
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT package_key, active
            FROM shipment_package_snapshots
            WHERE platform_order_no = ?
            ORDER BY package_key
            """,
            (platform,),
        ).fetchall()
    assert rows == [("10001:WO-STOPPED", 0), ("10001:WO-VALID", 1)]


def test_notification_sync_creates_two_of_seven_partial_draft(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    platform = "112-1234567-1234567"

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **_kwargs):
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no=f"1000{index}",
                        order_number=platform,
                        payload={},
                    )
                    for index in range(1, 8)
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": f"1000{index}",
                        "platform_order_no": platform,
                        "wo_number": f"WO-{index}",
                        "consignee": "Customer",
                        "logistics_type_name": "FedEx",
                        "waybill_no": f"TRACK-{index}",
                        "tracking_no": f"TRACK-{index}",
                    }
                    for index in (1, 2)
                ]
            )

    result = asyncio.run(sync_notification_drafts(_Gateway(), store, _config()))

    assert result["new_draft_count"] == 1
    assert result["partial_logistics_order_count"] == 1
    assert result["missing_system_order_count"] == 5
    notification = store.get_latest_notification(platform)
    assert notification is not None
    assert notification["package_total"] == 7
    assert notification["package_complete"] == 2
    assert notification["package_missing"] == 5
    assert len(notification["items"]) == 7
    pending = [
        item
        for item in notification["items"]
        if item["visibility_reason"] == "pending_wms"
    ]
    assert {item["system_order_no"] for item in pending} == {
        "10003",
        "10004",
        "10005",
        "10006",
        "10007",
    }
    assert all(item["package_snapshot_id"] is None for item in pending)
    assert all(item["is_complete"] == 0 for item in pending)


def test_notification_sync_isolates_one_platform_validation_failure(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    other_platform = "113-7654321-7654321"
    now = "2026-07-17T00:00:00Z"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO shipment_jobs (
                logistics_no, system_order_no, platform_order_no,
                shipment_tag_name, identity_state, first_seen_at,
                last_seen_at, created_at, updated_at
            ) VALUES ('ALS-OTHER', '30001', ?, 'tag', 'ACTIVE', ?, ?, ?, ?)
            """,
            (other_platform, now, now, now, now),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO shipment_erp ("
            "job_id, state, checkpoint, completion_source, updated_at"
            ") VALUES (?, 'DONE', 'OUTBOUNDED', 'AUTOMATION', ?)",
            (job_id, now),
        )
        conn.commit()

    class _Page:
        def __init__(self, items):
            self.items = items
            self.total = len(items)

    class _Gateway:
        async def list_orders(self, **kwargs):
            platform = kwargs["filters"]["platform_order_nos"][0]
            if platform == other_platform:
                raise RuntimeError("one order detail is temporarily unavailable")
            return _Page(
                [
                    SimpleNamespace(
                        global_order_no="10001",
                        order_number=platform,
                        payload={},
                    )
                ]
            )

        async def list_wms_orders(self, **_kwargs):
            return _Page(
                [
                    {
                        "order_number": "10001",
                        "platform_order_no": "112-1234567-1234567",
                        "wo_number": "WO-1",
                        "consignee": "Customer",
                        "logistics_type_name": "FedEx",
                        "waybill_no": "TRACK-1",
                    }
                ]
            )

    result = asyncio.run(sync_notification_drafts(_Gateway(), store, _config()))

    assert result["eligible_order_count"] == 2
    assert result["failed_order_count"] == 1
    assert result["new_draft_count"] == 1
    assert store.get_latest_notification("112-1234567-1234567") is not None
    assert store.get_latest_notification(other_platform) is None


def test_partial_notification_keeps_old_tracking_when_a_new_system_appears(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path, system_count=1)
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(system_order_nos=("10001", "20001"))
    )
    first_package = _package(1)
    first_merge = store.merge_package_scan(
        platform,
        [first_package],
        ("10001", "20001"),
    )
    assert first_merge["package_total"] == 2
    assert first_merge["package_complete"] == 1
    assert first_merge["package_missing"] == 1
    first = store.prepare_notification(platform, _config())
    assert first is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_notifications SET state = 'DELIVERED' WHERE id = ?",
            (first["id"],),
        )
        conn.commit()

    second_package = PackageSnapshot(
        package_key="20001:WO-2",
        platform_order_no=platform,
        system_order_no="20001",
        shipment_type=PACKAGE_OVERSEAS_AUTO,
        carrier_raw="UPS",
        carrier="UPS",
        tracking_no="1Z-NEW",
        final_tracking_no="1Z-NEW",
    )
    second_merge = store.merge_package_scan(
        platform,
        [second_package],
        ("10001", "20001"),
    )
    assert second_merge["preserved_package_count"] == 1
    assert second_merge["package_total"] == 2
    assert second_merge["package_complete"] == 2
    assert second_merge["package_missing"] == 0

    supplement = store.prepare_notification(platform, _config())
    assert supplement is not None
    assert supplement["id"] != first["id"]
    assert supplement["is_supplemental_revision"] is True
    assert {item["final_tracking_no"] for item in supplement["items"]} == {
        "TRACK-1",
        "1Z-NEW",
    }
    assert "TRACK-1" in supplement["body"]
    assert "1Z-NEW" in supplement["body"]

    # Startup reconciliation must preserve this genuine missing-package
    # supplement instead of treating it as a duplicate of the first send.
    reloaded = ShipmentNotificationStore(path)
    reloaded_supplement = reloaded.get_latest_notification(platform)
    assert reloaded_supplement is not None
    assert reloaded_supplement["id"] == supplement["id"]
    assert reloaded_supplement["state"] == supplement["state"]
    assert reloaded_supplement["state"] != NOTIFICATION_SUPPRESSED

    unchanged_merge = store.merge_package_scan(
        platform,
        [],
        ("10001", "20001"),
    )
    assert unchanged_merge["preserved_package_count"] == 2
    assert unchanged_merge["package_complete"] == 2
    unchanged = store.prepare_notification(platform, _config())
    assert unchanged is not None
    assert unchanged["id"] == supplement["id"]


def test_existing_unsent_revision_after_delivery_is_suppressed_on_initialize(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path)
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6)))
    )
    store.replace_package_scan(platform, [_package(index) for index in range(1, 6)])
    delivered = store.prepare_notification(platform, _config())
    assert delivered is not None

    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_notifications SET state = 'DELIVERED' WHERE id = ?",
            (delivered["id"],),
        )
        row = conn.execute(
            "SELECT * FROM shipment_notifications WHERE id = ?",
            (delivered["id"],),
        ).fetchone()
        assert row is not None
        columns = [name for name in row.keys() if name != "id"]
        values = [row[name] for name in columns]
        values[columns.index("revision")] = 2
        values[columns.index("state")] = NOTIFICATION_AWAITING_REVIEW
        values[columns.index("idempotency_key")] = "regenerated-after-delivery"
        values[columns.index("provider_message_id")] = None
        values[columns.index("provider_status")] = None
        values[columns.index("sent_at")] = None
        placeholders = ",".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO shipment_notifications ({','.join(columns)}) "
            f"VALUES ({placeholders})",
            values,
        )
        conn.commit()

    reloaded = ShipmentNotificationStore(path)
    latest = reloaded.get_latest_notification(platform)

    assert latest is not None
    assert latest["state"] == NOTIFICATION_SUPPRESSED
    assert latest["provider_status"] == "PREVIOUSLY_SENT"
    assert latest["reviews"][-1]["action"] == "AUTO_SUPPRESS_ALREADY_SENT"


def test_complete_delivered_notification_does_not_requeue_after_tracking_change(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    store = _ready_database(path)
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6)))
    )
    packages = [_package(index) for index in range(1, 6)]
    store.replace_package_scan(platform, packages)
    delivered = store.prepare_notification(platform, _config())
    assert delivered is not None

    with store.connect() as conn:
        conn.execute(
            """
            UPDATE shipment_notifications
            SET state = 'DELIVERED', provider_message_id = 'already-sent',
                sent_at = '2026-07-29T00:00:00+00:00'
            WHERE id = ?
            """,
            (delivered["id"],),
        )
        conn.commit()

    changed = list(packages)
    changed[-1] = PackageSnapshot(
        **{
            **changed[-1].__dict__,
            "tracking_no": "TRACK-5-REFRESHED",
            "final_tracking_no": "TRACK-5-REFRESHED",
        }
    )
    store.replace_package_scan(platform, changed)

    prepared = store.prepare_notification(platform, _config())
    assert prepared is not None
    assert prepared["id"] == delivered["id"]
    assert prepared["state"] == "DELIVERED"
    with store.connect() as conn:
        count = conn.execute(
            """
            SELECT COUNT(*)
            FROM shipment_notifications
            WHERE platform_order_no = ? AND legacy_email_batch_id IS NULL
            """,
            (platform,),
        ).fetchone()[0]
    assert count == 1

    store.replace_package_scan(platform, [*changed, _package(6)])
    supplement = store.prepare_notification(platform, _config())
    assert supplement is not None
    assert supplement["id"] != delivered["id"]
    assert supplement["is_supplemental_revision"] is True
    assert "TRACK-6" in supplement["body"]


def test_notification_scan_lock_is_single_owner_and_recoverable(tmp_path) -> None:
    store = ShipmentNotificationStore(tmp_path / "notification-lock.sqlite3")
    store.initialize()

    assert store.try_acquire_scan_lock("scan-a") is True
    assert store.try_acquire_scan_lock("scan-b") is False
    assert store.release_scan_lock("scan-b") is False
    assert store.release_scan_lock("scan-a") is True
    assert store.try_acquire_scan_lock("scan-b") is True
    assert store.release_scan_lock("scan-b") is True


class _AcceptedMail:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.receipt_status = "posting"

    async def send(self, **kwargs: Any) -> ProviderAcceptance:
        self.calls.append(kwargs)
        return ProviderAcceptance("provider-message-1", "ACCEPTED")

    async def receipt(self, **_kwargs: Any) -> dict[str, str]:
        return {
            "send_status": self.receipt_status,
            "message_id": "provider-message-sent-1",
        }


class _HistoryMail(_AcceptedMail):
    def __init__(self, statuses: list[str | Exception]) -> None:
        super().__init__()
        self.statuses = list(statuses)
        self.receipt_calls = 0

    async def receipt(self, **_kwargs: Any) -> dict[str, str]:
        self.receipt_calls += 1
        value = self.statuses.pop(0) if self.statuses else "posting"
        if isinstance(value, Exception):
            raise value
        return {
            "send_status": value,
            "message_id": "provider-message-sent-1",
        }


def _email_notification(tmp_path, *, name: str = "email.sqlite3"):
    store = _ready_database(tmp_path / name, system_count=1)
    store.upsert_contact(_contact(system_order_nos=("10001",)))
    store.replace_package_scan("112-1234567-1234567", [_package(1)])
    notification = store.prepare_notification("112-1234567-1234567", _config())
    assert notification is not None
    assert notification["channel"] == CHANNEL_EMAIL
    return store, notification


def test_provider_acceptance_persists_receipt_schedule_and_operator(tmp_path) -> None:
    store, notification = _email_notification(tmp_path, name="receipt-schedule.sqlite3")
    service = ShipmentNotificationService(
        store,
        _config(),
        alimail_client=_AcceptedMail(),  # type: ignore[arg-type]
    )

    accepted = asyncio.run(
        service.approve_and_send(notification["id"], actor="Operator@BillyPrint.com")
    )

    assert accepted["state"] == "ACCEPTED"
    assert accepted["provider_operator_email"] == "operator@billyprint.com"
    assert accepted["receipt_next_check_at"] > accepted["sent_at"]
    assert accepted["receipt_deadline_at"] > accepted["receipt_next_check_at"]
    assert accepted["receipt_check_attempt_count"] == 0


def test_old_posting_receipt_becomes_unconfirmed_without_retry(tmp_path) -> None:
    store, notification = _email_notification(tmp_path, name="receipt-deadline.sqlite3")
    mail = _AcceptedMail()
    service = ShipmentNotificationService(
        store,
        _config(),
        alimail_client=mail,  # type: ignore[arg-type]
    )
    accepted = asyncio.run(service.approve_and_send(notification["id"]))
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE shipment_notifications
            SET sent_at = '2026-08-01T00:00:00Z',
                receipt_deadline_at = '2026-08-02T00:00:00Z',
                receipt_next_check_at = ''
            WHERE id = ?
            """,
            (accepted["id"],),
        )
        conn.commit()

    result = asyncio.run(service.refresh_pending_receipts())
    refreshed = store.get_notification(notification["id"])

    assert result["checked"] == 1
    assert result["unconfirmed"] == 1
    assert refreshed is not None
    assert refreshed["state"] == NOTIFICATION_DELIVERY_UNCONFIRMED
    assert refreshed["provider_status"] == "posting"
    assert refreshed["receipt_check_attempt_count"] == 1
    assert refreshed["receipt_next_check_at"] == ""
    assert "不会自动重发" in refreshed["last_error"]


def test_receipt_refresh_reports_provider_error_reasons(tmp_path) -> None:
    store, notification = _email_notification(tmp_path, name="receipt-error.sqlite3")
    mail = _HistoryMail(
        [
            NotificationProviderError(
                "Alimail request failed with HTTP 404 "
                "(code=Error.InvalidId; request_id=req-safe)."
            )
        ]
    )
    service = ShipmentNotificationService(
        store,
        _config(),
        alimail_client=mail,  # type: ignore[arg-type]
    )
    asyncio.run(service.approve_and_send(notification["id"]))

    result = asyncio.run(service.refresh_pending_receipts())

    assert result["checked"] == 0
    assert result["errors"] == 1
    assert result["error_reasons"] == [
        {
            "reason": (
                "Alimail request failed with HTTP 404 "
                "(code=Error.InvalidId; request_id=req-safe)."
            ),
            "count": 1,
        }
    ]


def test_controller_receipt_refresh_shows_safe_provider_reason(
    tmp_path,
    monkeypatch,
) -> None:
    import shipment_automation.notification_service as notification_service_module

    store = _ready_database(tmp_path / "receipt-controller.sqlite3")
    logs: list[tuple[Any, ...]] = []

    class _ReceiptService:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def refresh_pending_receipts(self) -> dict[str, Any]:
            return {
                "checked": 1,
                "completed": 0,
                "retryable": 0,
                "status_check_failed": 0,
                "unconfirmed": 1,
                "errors": 14,
                "error_reasons": [
                    {
                        "reason": (
                            "Alimail request failed with HTTP 404 "
                            "(code=Error.InvalidId; request_id=req-safe)."
                        ),
                        "count": 14,
                    }
                ],
            }

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        notification_service_module,
        "ShipmentNotificationService",
        _ReceiptService,
    )
    fake = SimpleNamespace(
        _shipment_notification_context=lambda: (store, _config()),
        _state=SimpleNamespace(
            settings=SimpleNamespace(api_timeout_seconds=30),
        ),
        _append_log=lambda *args: logs.append(args),
    )

    result = PersistentBackgroundTaskController.refresh_shipment_notification_receipts(
        fake
    )

    assert result.accepted is False
    assert "查询请求失败 14 条" in result.message
    assert "HTTP 404" in result.message
    assert "Error.InvalidId" in result.message
    assert "14 条" in result.message
    assert "未发送任何邮件或短信" in result.message
    assert logs and logs[0][0].value == "WARNING"


def test_receipt_check_lease_prevents_duplicate_provider_queries(tmp_path) -> None:
    store, notification = _email_notification(tmp_path, name="receipt-lease.sqlite3")
    service = ShipmentNotificationService(
        store,
        _config(),
        alimail_client=_AcceptedMail(),  # type: ignore[arg-type]
    )
    asyncio.run(service.approve_and_send(notification["id"]))

    assert store.claim_receipt_check(notification["id"], owner="worker-a") is True
    assert store.claim_receipt_check(notification["id"], owner="worker-b") is False


def test_alimail_poll_waits_until_send_history_reports_success(tmp_path) -> None:
    store, notification = _email_notification(tmp_path, name="mail-delivered.sqlite3")
    mail = _HistoryMail(["posting", "success"])

    async def no_sleep(_seconds: float) -> None:
        return None

    service = ShipmentNotificationService(
        store,
        _config(),
        alimail_client=mail,  # type: ignore[arg-type]
        delivery_poll_timeout_seconds=1,
        delivery_poll_interval_seconds=0.5,
        sleeper=no_sleep,
    )
    result = asyncio.run(service.approve_send_and_wait(notification["id"]))

    assert result["state"] == "DELIVERED"
    assert result["provider_status"] == "success"
    assert len(mail.calls) == 1
    assert mail.receipt_calls == 2


def test_alimail_poll_timeout_is_not_reported_as_send_failure(tmp_path) -> None:
    store, notification = _email_notification(tmp_path, name="mail-timeout.sqlite3")
    mail = _HistoryMail(["posting"])
    service = ShipmentNotificationService(
        store,
        _config(),
        alimail_client=mail,  # type: ignore[arg-type]
        delivery_poll_timeout_seconds=0,
    )

    result = asyncio.run(service.approve_send_and_wait(notification["id"]))

    assert result["state"] == "FAILED"
    assert str(result["last_error"]).startswith("状态核验超时：")
    assert "这不等于发送失败" in str(result["last_error"])
    assert len(mail.calls) == 1
    assert mail.receipt_calls == 1


class _HistorySMS:
    def __init__(self, history_rows: list[dict[str, str] | Exception]) -> None:
        self.history_rows = list(history_rows)
        self.send_calls = 0
        self.history_calls = 0

    async def send(self, **_kwargs: Any) -> ProviderAcceptance:
        self.send_calls += 1
        return ProviderAcceptance("sms-history-1", "SUCCESS")

    async def history(self, _message_id: str, **_kwargs: Any) -> dict[str, str]:
        self.history_calls += 1
        value = self.history_rows.pop(0) if self.history_rows else {}
        if isinstance(value, Exception):
            raise value
        return value


def _sms_notification(tmp_path, *, name: str = "sms.sqlite3"):
    store = _ready_database(tmp_path / name, system_count=1)
    store.upsert_contact(
        _contact(
            email="",
            email_presence=EMAIL_PRESENCE_NOT_PROVIDED,
            system_order_nos=("10001",),
        )
    )
    store.replace_package_scan("112-1234567-1234567", [_package(1)])
    notification = store.prepare_notification("112-1234567-1234567", _config())
    assert notification is not None
    assert notification["channel"] == CHANNEL_SMS
    return store, notification


def test_clicksend_history_poll_marks_sms_delivered(tmp_path) -> None:
    store, notification = _sms_notification(tmp_path, name="delivered.sqlite3")
    sms = _HistorySMS(
        [
            {"status": "Queued", "status_code": "200", "status_text": "Queued"},
            {
                "status": "Sent",
                "status_code": "201",
                "status_text": "Message delivered to the handset",
            },
        ]
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    service = ShipmentNotificationService(
        store,
        _config(),
        clicksend_client=sms,  # type: ignore[arg-type]
        delivery_poll_timeout_seconds=1,
        delivery_poll_interval_seconds=0.5,
        sleeper=no_sleep,
    )
    result = asyncio.run(service.approve_send_and_wait(notification["id"]))

    assert result["state"] == "DELIVERED"
    assert result["last_error"] is None
    assert sms.send_calls == 1
    assert sms.history_calls == 2


def test_clicksend_history_definitive_failure_is_retryable(tmp_path) -> None:
    store, notification = _sms_notification(tmp_path, name="failed.sqlite3")
    sms = _HistorySMS(
        [
            {
                "status": "Failed",
                "status_code": "301",
                "status_text": "Invalid recipient",
            }
        ]
    )
    service = ShipmentNotificationService(
        store,
        _config(),
        clicksend_client=sms,  # type: ignore[arg-type]
        delivery_poll_timeout_seconds=0,
    )

    result = asyncio.run(service.approve_send_and_wait(notification["id"]))

    assert result["state"] == "RETRYABLE"
    assert str(result["last_error"]).startswith("发送失败：ClickSend 明确返回")


@pytest.mark.parametrize(
    ("history_rows", "error_prefix"),
    [
        ([{"status": "Queued", "status_code": "200"}], "状态核验超时："),
        (
            [NotificationProviderError("history unavailable", retryable=True)],
            "状态查询失败：",
        ),
    ],
)
def test_delivery_poll_exhaustion_is_distinct_from_send_failure(
    tmp_path, history_rows, error_prefix
) -> None:
    store, notification = _sms_notification(tmp_path, name=error_prefix[:2] + ".sqlite3")
    sms = _HistorySMS(history_rows)
    service = ShipmentNotificationService(
        store,
        _config(),
        clicksend_client=sms,  # type: ignore[arg-type]
        delivery_poll_timeout_seconds=0,
    )

    result = asyncio.run(service.approve_send_and_wait(notification["id"]))

    assert result["state"] == "FAILED"
    assert str(result["last_error"]).startswith(error_prefix)
    assert "这不等于发送失败" in str(result["last_error"])
    assert result["provider_message_id"] == "sms-history-1"


def test_external_send_is_only_reached_after_atomic_review(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    store.upsert_contact(_contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6))))
    store.replace_package_scan(
        "112-1234567-1234567", [_package(index) for index in range(1, 6)]
    )
    notification = store.prepare_notification("112-1234567-1234567", _config())
    assert notification is not None
    mail = _AcceptedMail()
    service = ShipmentNotificationService(
        store,
        _config(),
        alimail_client=mail,  # type: ignore[arg-type]
    )
    assert mail.calls == []
    accepted = asyncio.run(service.approve_and_send(notification["id"]))
    assert accepted["state"] == "ACCEPTED"
    assert len(mail.calls) == 1
    assert mail.calls[0]["body_html"] == notification["body_html"]
    assert "<a href=" in mail.calls[0]["body_html"]


def test_provider_guard_never_emails_an_amazon_country_virtual_address(tmp_path) -> None:
    store, notification = _email_notification(
        tmp_path,
        name="amazon-virtual-provider-guard.sqlite3",
    )
    claimed = store.approve_and_claim(notification["id"], _config())
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE shipment_notifications
            SET target = ?, recipient_email = ?
            WHERE id = ?
            """,
            (
                "alias@marketplace.amazon.ca",
                "alias@marketplace.amazon.ca",
                notification["id"],
            ),
        )
    corrupted_claim = store.get_notification(notification["id"])
    assert corrupted_claim is not None
    assert claimed["state"] == "SENDING"
    mail = _AcceptedMail()
    service = ShipmentNotificationService(
        store,
        _config(),
        alimail_client=mail,  # type: ignore[arg-type]
    )

    with pytest.raises(NotificationProviderError, match="Amazon 虚拟邮箱"):
        asyncio.run(service._send_claimed(corrupted_claim))

    assert mail.calls == []
    failed = store.get_notification(notification["id"])
    assert failed is not None
    assert failed["state"] == "FAILED"
    assert "已禁止邮件发送" in str(failed["last_error"])


def test_approved_retry_keeps_exact_legacy_body_when_only_template_changed(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6)))
    )
    store.replace_package_scan(platform, [_package(index) for index in range(1, 6)])
    notification = store.prepare_notification(platform, _config())
    assert notification is not None
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE shipment_notifications
            SET state = 'RETRYABLE', template_version = 'shipment-email-v2',
                body = 'Legacy approved body', body_html = '',
                content_hash = 'legacy-approved-hash',
                approved_content_hash = 'legacy-approved-hash'
            WHERE id = ?
            """,
            (notification["id"],),
        )
        conn.commit()

    unchanged = store.prepare_notification(platform, _config())
    assert unchanged is not None
    assert unchanged["id"] == notification["id"]
    assert unchanged["body"] == "Legacy approved body"

    mail = _AcceptedMail()
    service = ShipmentNotificationService(
        store,
        _config(),
        alimail_client=mail,  # type: ignore[arg-type]
    )
    accepted = asyncio.run(service.retry_approved_content(notification["id"]))
    assert accepted["state"] == "ACCEPTED"
    assert mail.calls[0]["body"] == "Legacy approved body"
    assert mail.calls[0]["body_html"] == ""


def test_terminal_history_is_not_reopened_for_template_only_change(tmp_path) -> None:
    store = _ready_database(tmp_path / "queue.sqlite3")
    platform = "112-1234567-1234567"
    store.upsert_contact(
        _contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6)))
    )
    store.replace_package_scan(platform, [_package(index) for index in range(1, 6)])
    notification = store.prepare_notification(platform, _config())
    assert notification is not None
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE shipment_notifications
            SET state = 'DELIVERED', template_version = 'shipment-email-v2',
                body_html = '', content_hash = 'legacy-terminal-hash'
            WHERE id = ?
            """,
            (notification["id"],),
        )
        conn.commit()

    unchanged = store.prepare_notification(platform, _config())
    assert unchanged is not None
    assert unchanged["id"] == notification["id"]
    assert len(store.list_notifications(latest_only=False)) == 1


def test_current_schema_keeps_prior_fields_and_adds_service_line(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        logistics_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(shipment_logistics)")
        }
        assert "service_line" in logistics_columns
        notification_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(shipment_notifications)")
        }
        item_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(shipment_notification_items)")
        }
        assert "body_html" in notification_columns
        assert "product_names_json" in notification_columns
        assert {
            "provider_operator_email",
            "receipt_next_check_at",
            "receipt_last_checked_at",
            "receipt_deadline_at",
            "receipt_check_attempt_count",
            "receipt_check_lease_owner",
            "receipt_check_lease_until",
        }.issubset(notification_columns)
        assert "tracking_url" in item_columns
        assert {"customer_visible", "visibility_reason"}.issubset(item_columns)
        product_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(shipment_order_product_snapshots)"
            )
        }
        assert {
            "source_sequence",
            "local_sku",
            "raw_title",
            "display_title",
            "has_main_image",
            "metadata_valid",
            "is_instruction",
        }.issubset(product_columns)
        erp_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(shipment_erp)")
        }
        assert {
            "selected_wms_wo_number",
            "selected_wms_candidates_hash",
            "selected_wms_selected_at",
            "selected_wms_selected_by",
        }.issubset(erp_columns)
        conn.execute("PRAGMA user_version = 10")
        conn.commit()

    ShipmentWorkflowStore(path).initialize()
    backups = list(tmp_path.glob("queue.pre_v11_*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10


def test_v11_database_is_backed_up_before_v12_product_migration(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE shipment_order_product_snapshots")
        conn.execute("PRAGMA user_version = 11")
        conn.commit()

    ShipmentWorkflowStore(path).initialize()

    backups = list(tmp_path.glob("queue.pre_v12_*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='shipment_order_product_snapshots'"
        ).fetchone() is None


def test_v13_database_is_backed_up_before_v14_receipt_migration(tmp_path) -> None:
    path = tmp_path / "queue.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    receipt_columns = (
        "provider_operator_email",
        "receipt_next_check_at",
        "receipt_last_checked_at",
        "receipt_deadline_at",
        "receipt_check_attempt_count",
        "receipt_check_lease_owner",
        "receipt_check_lease_until",
    )
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_shipment_notifications_receipt_due")
        for column in receipt_columns:
            conn.execute(f"ALTER TABLE shipment_notifications DROP COLUMN {column}")
        conn.execute("PRAGMA user_version = 13")
        conn.commit()

    ShipmentWorkflowStore(path).initialize()

    backups = list(tmp_path.glob("queue.pre_v14_*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(shipment_notifications)")
        }
        assert set(receipt_columns).issubset(columns)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_v14_database_is_backed_up_before_v15_recipient_choice_migration(
    tmp_path,
) -> None:
    path = tmp_path / "queue.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE shipment_notification_recipient_name_choices")
        conn.execute("PRAGMA user_version = 14")
        conn.commit()

    ShipmentWorkflowStore(path).initialize()

    backups = list(tmp_path.glob("queue.pre_v15_*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='shipment_notification_recipient_name_choices'"
        ).fetchone() is None
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='shipment_notification_recipient_name_choices'"
        ).fetchone() is not None
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize(
    ("provider_status", "expected_state"),
    [("posting", "ACCEPTED"), ("success", "DELIVERED"), ("failed", "RETRYABLE")],
)
def test_alimail_send_status_updates_local_notification(
    tmp_path, provider_status: str, expected_state: str
) -> None:
    store = _ready_database(tmp_path / f"{provider_status}.sqlite3")
    store.upsert_contact(
        _contact(system_order_nos=tuple(f"1000{i}" for i in range(1, 6)))
    )
    store.replace_package_scan(
        "112-1234567-1234567", [_package(index) for index in range(1, 6)]
    )
    notification = store.prepare_notification("112-1234567-1234567", _config())
    assert notification is not None
    mail = _AcceptedMail()
    service = ShipmentNotificationService(
        store,
        _config(),
        alimail_client=mail,  # type: ignore[arg-type]
    )
    accepted = asyncio.run(service.approve_and_send(notification["id"]))
    assert accepted["state"] == "ACCEPTED"

    mail.receipt_status = provider_status
    refreshed = asyncio.run(service.refresh_delivery_receipt(notification["id"]))

    assert refreshed["state"] == expected_state
    assert refreshed["provider_status"] == provider_status
    assert refreshed["provider_message_id"] == "provider-message-sent-1"
    with pytest.raises(Exception):
        asyncio.run(service.approve_and_send(notification["id"]))
    assert len(mail.calls) == 1


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload


class _AlimailHTTP:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[tuple[str, dict[str, Any]]] = []
        self.messages = messages

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append((url, kwargs))
        if url.endswith("/token"):
            return _Response({"access_token": "token", "expires_in": 3600})
        if "/messages/query" in url:
            return _Response(
                {
                    "messages": self.messages if self.messages is not None else [
                        {
                            "id": "sent-1",
                            "internetMessageId": (
                                "<stable-key@shipment-automation.billyprint.com>"
                            ),
                            "sendStatus": "success",
                        }
                    ]
                }
            )
        if url.endswith("/messages"):
            return _Response({"message": {"id": "draft-1"}})
        return _Response({})

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.gets.append((url, kwargs))
        return _Response({"message": {"sendStatus": "success"}})


class _AlimailSentCopyHTTP(_AlimailHTTP):
    """Replay the documented sent-copy response after the draft id expires."""

    def __init__(
        self,
        *,
        ambiguous: bool = False,
        candidate_overrides: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(messages=[])
        self.ambiguous = ambiguous
        self.candidate_overrides = dict(candidate_overrides or {})

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append((url, kwargs))
        if url.endswith("/token"):
            return _Response({"access_token": "token", "expires_in": 3600})
        if "/messages/query" in url:
            query = str(kwargs["json"]["query"])
            order_no = query.split("Shipment Update - ", 1)[1].split('"', 1)[0]
            item = {
                "id": f"sent-{order_no}",
                # Alibaba Mail can return a sent-copy Message-ID that differs
                # from the custom value submitted when creating the draft.
                "internetMessageId": f"<provider-{order_no}@alimail>",
                "subject": f"Shipment Update - {order_no}",
                "toRecipients": [
                    {"email": f"customer-{order_no}@example.com", "name": "Customer"}
                ],
                "folderId": "1",
                "sendStatus": "success",
                "sentDateTime": "2026-08-05T09:38:00Z",
            }
            item.update(self.candidate_overrides)
            messages = [item]
            if self.ambiguous:
                messages.append({**item, "id": f"duplicate-{order_no}"})
            return _Response({"messages": messages, "total": len(messages), "nextCursor": "$"})
        return _Response({})

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.gets.append((url, kwargs))
        return _Response(
            {
                "code": "Error.InvalidId",
                "message": "The draft message no longer exists.",
                "requestId": "req-draft-gone",
            },
            status_code=404,
        )


def test_alimail_uses_selected_sender_in_both_paths_and_message() -> None:
    async def run() -> None:
        http = _AlimailHTTP()
        client = AlimailClient("id", "secret", http_client=http)
        result = await client.send(
            sender_email="acs@billyprint.com",
            sender_name="BillyPrint Customer Service",
            recipient_email="customer@example.com",
            recipient_name="Customer",
            subject="Shipment Update",
            body="Body",
            idempotency_key="stable-key",
            body_html='<p>Reviewed <a href="https://example.com">link</a></p>',
        )
        assert result.message_id == "draft-1"
        assert "/v2/users/acs@billyprint.com/messages" in http.posts[1][0]
        assert "/v2/users/acs@billyprint.com/messages/draft-1/send" in http.posts[2][0]
        message = http.posts[1][1]["json"]["message"]
        assert message["from"]["email"] == "acs@billyprint.com"
        assert message["replyTo"][0]["email"] == "acs@billyprint.com"
        assert message["body"]["bodyText"] == "Body"
        assert message["body"]["bodyHtml"] == (
            '<p>Reviewed <a href="https://example.com">link</a></p>'
        )

    asyncio.run(run())


def test_alimail_receipt_never_uses_a_nearby_nonmatching_message() -> None:
    async def run() -> None:
        http = _AlimailHTTP(
            messages=[
                {
                    "id": "someone-elses-message",
                    "internetMessageId": "<different-key@example.com>",
                    "sendStatus": "success",
                    "sentDateTime": "2026-08-03T13:35:58Z",
                }
            ]
        )
        client = AlimailClient("id", "secret", http_client=http)
        receipt = await client.receipt(
            sender_email="acs@billyprint.com",
            message_id="draft-1",
            idempotency_key="stable-key",
            subject="Shipment Update - 702-3058964-4962622",
            sent_at="2026-08-03T13:35:58Z",
        )

        assert receipt == {"send_status": "success", "message_id": "draft-1"}
        assert len(http.gets) == 1

    asyncio.run(run())


def test_alimail_receipt_requests_only_send_status_for_created_message() -> None:
    async def run() -> None:
        http = _AlimailHTTP()
        client = AlimailClient("id", "secret", http_client=http)
        receipt = await client.receipt(
            sender_email="acs@billyprint.com",
            message_id="draft-1",
            idempotency_key="stable-key",
            subject="Shipment Update - 112-1234567-1234567",
        )

        assert receipt == {
            "send_status": "success",
            "message_id": "sent-1",
            "match_source": "exact_internet_message_id",
        }
        search_url, search_request = http.posts[1]
        assert "/v2/users/acs@billyprint.com/messages/query" in search_url
        assert (
            "$select=id,internetMessageId,subject,toRecipients,folderId,"
            "sendStatus,sentDateTime"
        ) in search_url
        assert "email" not in search_request["json"]
        assert search_request["json"]["query"] == (
            'subject:"Shipment Update - 112-1234567-1234567"'
        )
        assert http.gets == []

    asyncio.run(run())


def test_alimail_receipt_recovers_unique_documented_sent_copy() -> None:
    async def run() -> None:
        http = _AlimailSentCopyHTTP()
        client = AlimailClient("id", "secret", http_client=http)
        receipt = await client.receipt(
            sender_email="acs@billyprint.com",
            message_id="draft-1",
            idempotency_key="stable-key",
            subject="Shipment Update - 112-1234567-1234567",
            recipient_email="customer-112-1234567-1234567@example.com",
            sent_at="2026-08-05T09:38:03Z",
        )

        assert receipt == {
            "send_status": "success",
            "message_id": "sent-112-1234567-1234567",
            "match_source": "unique_sent_copy",
        }
        search_url, search_request = http.posts[1]
        assert "$select=id,internetMessageId,subject,toRecipients,folderId," in search_url
        assert "email" not in search_request["json"]
        query = search_request["json"]["query"]
        assert "folderId:1" in query
        assert 'subject:"Shipment Update - 112-1234567-1234567"' in query
        assert 'toEmail="customer-112-1234567-1234567@example.com"' in query
        assert "date>=2026-08-05T09:23:03Z" in query
        assert "date<=2026-08-05T09:53:03Z" in query
        assert http.gets == []

    asyncio.run(run())


def test_alimail_receipt_replays_fourteen_failed_production_queries() -> None:
    async def run() -> None:
        http = _AlimailSentCopyHTTP()
        client = AlimailClient("id", "secret", http_client=http)
        receipts = []
        for index in range(14):
            order_no = f"112-9000000-{index:07d}"
            receipts.append(
                await client.receipt(
                    sender_email="acs@billyprint.com",
                    message_id=f"draft-{index}",
                    idempotency_key=f"stable-key-{index}",
                    subject=f"Shipment Update - {order_no}",
                    recipient_email=f"customer-{order_no}@example.com",
                    sent_at="2026-08-05T09:38:03Z",
                )
            )

        assert len(receipts) == 14
        assert {item["match_source"] for item in receipts} == {"unique_sent_copy"}
        assert all(item["send_status"] == "success" for item in receipts)
        assert http.gets == []

    asyncio.run(run())


def test_alimail_receipt_rejects_ambiguous_sent_copy_matches() -> None:
    async def run() -> None:
        http = _AlimailSentCopyHTTP(ambiguous=True)
        client = AlimailClient("id", "secret", http_client=http)
        with pytest.raises(NotificationProviderError) as captured:
            await client.receipt(
                sender_email="acs@billyprint.com",
                message_id="draft-1",
                idempotency_key="stable-key",
                subject="Shipment Update - 112-1234567-1234567",
                recipient_email="customer-112-1234567-1234567@example.com",
                sent_at="2026-08-05T09:38:03Z",
            )

        assert "HTTP 404" in str(captured.value)
        assert len(http.gets) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "candidate_overrides",
    (
        {"folderId": "2"},
        {"subject": "Shipment Update - another-order"},
        {"toRecipients": [{"email": "another@example.com", "name": "Other"}]},
        {"sentDateTime": "2026-08-05T10:38:03Z"},
    ),
)
def test_alimail_receipt_rejects_sent_copy_outside_safe_identity(
    candidate_overrides: dict[str, Any],
) -> None:
    async def run() -> None:
        http = _AlimailSentCopyHTTP(candidate_overrides=candidate_overrides)
        client = AlimailClient("id", "secret", http_client=http)
        with pytest.raises(NotificationProviderError) as captured:
            await client.receipt(
                sender_email="acs@billyprint.com",
                message_id="draft-1",
                idempotency_key="stable-key",
                subject="Shipment Update - 112-1234567-1234567",
                recipient_email="customer-112-1234567-1234567@example.com",
                sent_at="2026-08-05T09:38:03Z",
            )

        assert "HTTP 404" in str(captured.value)
        assert len(http.gets) == 1

    asyncio.run(run())


class _AlimailForbiddenHTTP:
    async def post(self, url: str, **_kwargs: Any) -> _Response:
        if url.endswith("/token"):
            return _Response({"access_token": "token", "expires_in": 3600})
        return _Response(
            {
                "code": "Forbidden.Operation",
                "message": (
                    "sender customer@example.com phone +14155552671 "
                    "access_token=should-not-leak is not authorized"
                ),
                "requestId": "req_ABC-123",
            },
            status_code=403,
        )


def test_alimail_forbidden_error_is_safe_and_retryable() -> None:
    async def run() -> None:
        client = AlimailClient("id", "secret", http_client=_AlimailForbiddenHTTP())
        with pytest.raises(NotificationProviderError) as captured:
            await client.send(
                sender_email="acs@billyprint.com",
                sender_name="BillyPrint Customer Service",
                recipient_email="customer@example.com",
                recipient_name="Customer",
                subject="Shipment Update",
                body="Body",
                idempotency_key="stable-key",
            )
        error = captured.value
        assert error.retryable is True
        assert "HTTP 403" in str(error)
        assert "code=Forbidden.Operation" in str(error)
        assert "request_id=req_ABC-123" in str(error)
        assert "customer@example.com" not in str(error)
        assert "+14155552671" not in str(error)
        assert "should-not-leak" not in str(error)

    asyncio.run(run())


class _ClickSendHTTP:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.requests.append((method, url, kwargs))
        return _Response(
            {
                "response_code": "SUCCESS",
                "data": {
                    "messages": [
                        {"message_id": "sms-1", "status": "SUCCESS"}
                    ]
                },
            }
        )


def test_clicksend_payload_contains_deterministic_reference() -> None:
    async def run() -> None:
        http = _ClickSendHTTP()
        client = ClickSendClient("username", "key", http_client=http)
        result = await client.send(to="+14155552671", body="Body", idempotency_key="stable-key")
        assert result.message_id == "sms-1"
        message = http.requests[0][2]["json"]["messages"][0]
        assert message == {
            "to": "+14155552671",
            "body": "Body",
            "source": "erp-shipment-automation",
            "custom_string": "stable-key",
        }

    asyncio.run(run())


def test_clicksend_history_finds_exact_message_without_receipt_rule() -> None:
    class HistoryHTTP:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, dict[str, Any]]] = []

        async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
            self.requests.append((method, url, kwargs))
            return _Response(
                {
                    "response_code": "SUCCESS",
                    "data": {
                        "last_page": 1,
                        "data": [
                            {
                                "message_id": "other-message",
                                "status": "Sent",
                                "status_code": "201",
                            },
                            {
                                "message_id": "sms-1",
                                "status": "Sent",
                                "status_code": "201",
                                "status_text": "Message delivered to the handset",
                            },
                        ],
                    },
                }
            )

    async def run() -> None:
        http = HistoryHTTP()
        client = ClickSendClient("username", "key", http_client=http)
        result = await client.history("SMS-1", sent_at="2026-07-21T07:54:13Z")
        assert result["message_id"] == "sms-1"
        assert result["status_code"] == "201"
        method, url, kwargs = http.requests[0]
        assert method == "GET"
        assert url.endswith("/v3/sms/history")
        assert kwargs["params"]["limit"] == 100
        assert kwargs["params"]["order_by"] == "date:desc"

    asyncio.run(run())
