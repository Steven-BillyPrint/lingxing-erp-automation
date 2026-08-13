from __future__ import annotations

import asyncio
import builtins
import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from erp_automation.application.desktop_tasks import DesktopTaskRunner
from erp_automation.application.capabilities import CapabilityUnavailable
from erp_automation.application.lingxing_gateway import ResolvedOrderDetail
from erp_automation.persistence import (
    CustomWorkflowStore,
    StageRetryReviewResolution,
    WorkflowPauseKind,
    WorkflowStageState,
)
from erp_automation.ui.controller import ControlResult
from erp_automation.ui.models import (
    Capability,
    DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY,
    DESKTOP_OPERATOR_NAME_PAYLOAD_KEY,
    DesktopSettings,
    DesktopInteractionResponse,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
    TaskArea,
    TaskCommand,
    notification_confirmation_order_no,
)
from lingxing_automation.flows import contact_sync
from lingxing_automation.models import ContactInfo, FolderBuildResult, OrderFolderLine
from lingxing_automation.services.tent_package_split_planner import (
    TentPackageSplitItem,
    TentPackageSplitPackage,
    TentPackageSplitPlan,
)
from lingxing_automation.services.tent_sku_planner import (
    DestinationRegion,
    TentSkuAdjustmentPlan,
    TentSkuPlanAction,
    build_tent_sku_plan,
)
from shipment_automation import erp_mark_ship
from shipment_automation.alibaba_order_browser import (
    AlibabaDraftFacts,
    AlibabaDraftFillResult,
)
from shipment_automation.alibaba_order_session import AlibabaOrderSessionStore
from shipment_automation.alibaba_ordering import AlibabaRoute
from shipment_automation.models import (
    LOGISTICS_READY,
    LogisticsDetail,
    ShipmentCandidate,
)
from shipment_automation.queue_store import ShipmentWorkflowStore


PLATFORM_ORDER_NO = "111-2222222-3333333"
SYSTEM_ORDER_NO = "103000000000000001"


def _alibaba_order_detail() -> dict[str, Any]:
    return {
        "order_item": [
            {
                "MSKU": "Custom-Tent-Package-10x10",
                "sku": "10x10-Canopy-Topper",
            }
        ],
        "receive_info": {
            "receiver_name": "Jane Smith",
            "company_name": "",
            "country_code": "US",
            "country": "United States",
            "state": "CA",
            "city": "Los Angeles",
            "address_line1": "123 Main Street",
            "postal_code": "90012-1234",
            "phone_code": "1",
            "receiver_phone": "2135550188",
            "receiver_email": "jane@example.com",
        },
    }


def _draft_confirmation(order_no: str = SYSTEM_ORDER_NO) -> DesktopWriteConfirmation:
    return DesktopWriteConfirmation.create(
        DesktopWriteAction.FILL_ALIBABA_ORDER_DRAFT,
        order_no,
        system_order_no=order_no,
    )


def test_prepare_alibaba_order_reads_lingxing_and_opens_quote(
    tmp_path,
    monkeypatch,
) -> None:
    observed: dict[str, Any] = {}
    original_address_loader = DesktopTaskRunner._alibaba_shipping_address

    @asynccontextmanager
    async def fake_context(_endpoint):
        yield object()

    class FakeQuotePage:
        async def bring_to_front(self):
            observed["quote_brought_to_front"] = True

    class FakeBrowser:
        def __init__(self, _context):
            pass

        async def draft_urls(self):
            return ("https://scm.alibaba.com/web/express/order.htm?old=1",)

        async def prepare_quote_page(self, *, login_config):
            observed["quote_page_started"] = True
            await asyncio.sleep(0)
            assert observed.get("address_started") is True
            observed["login_config"] = login_config
            return FakeQuotePage()

    monkeypatch.setattr(
        "shipment_automation.alibaba_order_browser.attached_alibaba_context",
        fake_context,
    )
    monkeypatch.setattr(
        "shipment_automation.alibaba_order_browser.AlibabaOrderBrowser",
        FakeBrowser,
    )

    async def concurrent_address_loader(detail, context, system_order_no):
        observed["address_started"] = True
        await asyncio.sleep(0)
        assert observed.get("quote_page_started") is True
        return await original_address_loader(detail, context, system_order_no)

    monkeypatch.setattr(
        DesktopTaskRunner,
        "_alibaba_shipping_address",
        staticmethod(concurrent_address_loader),
    )

    async def lookup(_settings, system_order_no):
        observed["system_order_no"] = system_order_no
        return _alibaba_order_detail()

    async def interaction_handler(**kwargs):
        observed["quote_details"] = dict(kwargs.get("display_data") or {})
        observed["quote_details_non_blocking"] = kwargs.get("non_blocking")
        observed["quote_details_target"] = kwargs.get("target_instance_id")
        return DesktopInteractionResponse("quote-details", True)

    settings = replace(
        _settings(tmp_path),
        alibaba_account="configured@example.com",
        alibaba_password="configured-password",
        alibaba_auto_login=True,
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        order_detail_lookup=lookup,
        interaction_handler=interaction_handler,
    )
    result = runner(
        TaskCommand(
            "prepare",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_ORDER_PREPARE,
            order_no=SYSTEM_ORDER_NO,
            payload={
                "_desktop_browser_endpoint": "http://127.0.0.1:9222",
                "_desktop_instance_id": "desktop-a",
            },
        )
    )

    assert result.succeeded is True
    assert result.payload["category"] == "tent"
    assert result.payload["destination_country_code"] == "US"
    assert result.payload["quote_page_opened"] is True
    assert result.payload["quote_fields_prefilled"] is False
    assert result.payload["alibaba_submit_calls"] == 0
    assert observed["system_order_no"] == SYSTEM_ORDER_NO
    assert observed["quote_brought_to_front"] is True
    assert observed["quote_details"] == {
        "requested_order_no": SYSTEM_ORDER_NO,
        "system_order_no": SYSTEM_ORDER_NO,
        "platform_order_no": "",
        "origin_country": "中国大陆",
        "origin_city": "佛山市",
        "destination_country_code": "US",
        "destination_country_name": "United States",
        "destination_postal_code": "90012",
    }
    assert observed["quote_details_non_blocking"] is True
    assert observed["quote_details_target"] == "desktop-a"
    assert observed["login_config"].auto_login is True
    assert observed["login_config"].account == "configured@example.com"
    assert observed["login_config"].password == "configured-password"
    assert (
        AlibabaOrderSessionStore(
            tmp_path / "data" / "alibaba_ordering.sqlite3"
        ).get(SYSTEM_ORDER_NO, instance_id="desktop-a")
        is not None
    )


def test_prepare_alibaba_order_falls_back_to_verified_local_lingxing_address(
    tmp_path,
    monkeypatch,
) -> None:
    @asynccontextmanager
    async def fake_context(_endpoint):
        yield object()

    class FakeQuotePage:
        async def bring_to_front(self):
            pass

    class FakeAlibabaBrowser:
        def __init__(self, _context):
            pass

        async def draft_urls(self):
            return ()

        async def prepare_quote_page(self, *, login_config):
            assert login_config.auto_login is True
            return FakeQuotePage()

    class FakeLingxingBrowser:
        def __init__(self, _context):
            pass

        async def order_detail(self, system_order_no):
            assert system_order_no == SYSTEM_ORDER_NO
            return {
                "global_order_no": SYSTEM_ORDER_NO,
                "buyer_info": {"buyer_email": "jane@example.com"},
                "receive_info": {
                    "receiver_name": "Example Cooperative",
                    "receiver_country_code": "US",
                    "receiver_country_name": "United States of America (USA)",
                    "state_or_region": "FL",
                    "city": "MIAMI",
                    "postal_code": "33182-1909",
                    "receiver_mobile": "3055550199",
                    "address_line1": "987 Example Street Apt Unit 100",
                },
            }

    monkeypatch.setattr(
        "shipment_automation.alibaba_order_browser.attached_alibaba_context",
        fake_context,
    )
    monkeypatch.setattr(
        "shipment_automation.alibaba_order_browser.AlibabaOrderBrowser",
        FakeAlibabaBrowser,
    )
    monkeypatch.setattr(
        "shipment_automation.lingxing_order_browser.LingxingOrderBrowser",
        FakeLingxingBrowser,
    )
    detail = _alibaba_order_detail()
    detail["receive_info"]["address_line1"] = ""
    detail["receive_info"]["receiver_email"] = ""

    async def lookup(_settings, order_identifier):
        return ResolvedOrderDetail(
            requested_order_no=order_identifier,
            system_order_no=SYSTEM_ORDER_NO,
            platform_order_no=PLATFORM_ORDER_NO,
            payload=detail,
        )

    observed_quote_details: dict[str, str] = {}

    async def interaction_handler(**kwargs):
        observed_quote_details.update(kwargs.get("display_data") or {})
        return DesktopInteractionResponse("quote-details", True)

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        order_detail_lookup=lookup,
        interaction_handler=interaction_handler,
    )
    result = runner(
        TaskCommand(
            "prepare",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_ORDER_PREPARE,
            order_no=PLATFORM_ORDER_NO,
            payload={
                "_desktop_browser_endpoint": "http://127.0.0.1:9222",
                "_desktop_instance_id": "desktop-a",
            },
        )
    )

    assert result.succeeded is True
    assert result.payload["system_order_no"] == SYSTEM_ORDER_NO
    assert result.payload["address_source"] == "lingxing_web_detail_api"
    assert observed_quote_details["destination_country_code"] == "US"
    assert observed_quote_details["destination_postal_code"] == "33182"


def test_fill_alibaba_order_draft_uses_new_page_and_never_submits(
    tmp_path,
    monkeypatch,
) -> None:
    baseline = "https://scm.alibaba.com/web/express/order.htm?old=1"
    target = "https://scm.alibaba.com/web/express/order.htm?new=1"
    AlibabaOrderSessionStore(
        tmp_path / "data" / "alibaba_ordering.sqlite3"
    ).save(
        instance_id="desktop-a",
        system_order_no=SYSTEM_ORDER_NO,
        category="tent",
        baseline_draft_urls=(baseline,),
    )
    observed: dict[str, Any] = {}
    async def unexpected_wait_for(awaitable, *, timeout):
        if hasattr(awaitable, "close"):
            awaitable.close()
        pytest.fail(
            f"draft form fill must not be cancelled by a global timeout: {timeout}"
        )

    monkeypatch.setattr(
        "erp_automation.application.desktop_tasks.asyncio.wait_for",
        unexpected_wait_for,
    )
    original_address_loader = DesktopTaskRunner._alibaba_shipping_address

    @asynccontextmanager
    async def fake_context(_endpoint):
        yield object()

    class FakeBrowser:
        def __init__(self, _context):
            pass

        async def draft_urls(self):
            observed["draft_inspection_started"] = True
            await asyncio.sleep(0)
            assert observed.get("address_started") is True
            return baseline, target

        async def page_for_url(self, url):
            observed["target_url"] = url
            return object()

        async def ensure_logged_in(
            self,
            page,
            _login_config,
            *,
            return_url,
            page_label,
        ):
            observed["login_return_url"] = return_url
            observed["login_page_label"] = page_label
            return page

        async def inspect_draft(self, _page):
            return AlibabaDraftFacts(
                url=target,
                route=AlibabaRoute("Express Expedited"),
                total_weight_kg=Decimal("20"),
                route_is_expedited=True,
                signature_available=True,
            )

        async def fill_draft(self, _page, **kwargs):
            observed["fill_kwargs"] = kwargs
            return AlibabaDraftFillResult(
                url=target,
                route_name="Express Expedited",
                total_weight_kg=Decimal("20"),
                declared_unit_price_usd=kwargs[
                    "declaration"
                ].declared_unit_price_usd,
                signature_selected=kwargs["signature_requested"],
                signature_fee_text="",
            )

    monkeypatch.setattr(
        "shipment_automation.alibaba_order_browser.attached_alibaba_context",
        fake_context,
    )
    monkeypatch.setattr(
        "shipment_automation.alibaba_order_browser.AlibabaOrderBrowser",
        FakeBrowser,
    )

    async def concurrent_address_loader(detail, context, system_order_no):
        observed["address_started"] = True
        await asyncio.sleep(0)
        assert observed.get("draft_inspection_started") is True
        return await original_address_loader(detail, context, system_order_no)

    monkeypatch.setattr(
        DesktopTaskRunner,
        "_alibaba_shipping_address",
        staticmethod(concurrent_address_loader),
    )

    async def lookup(_settings, order_identifier):
        return ResolvedOrderDetail(
            requested_order_no=order_identifier,
            system_order_no=SYSTEM_ORDER_NO,
            platform_order_no=PLATFORM_ORDER_NO,
            payload=_alibaba_order_detail(),
        )

    confirmation = _draft_confirmation(PLATFORM_ORDER_NO)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        order_detail_lookup=lookup,
    )
    result = runner(
        TaskCommand(
            "fill",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_ORDER_DRAFT,
            order_no=PLATFORM_ORDER_NO,
            payload={
                "_desktop_browser_endpoint": "http://127.0.0.1:9222",
                "_desktop_instance_id": "desktop-a",
                "expedited": True,
                "signature_requested": False,
                "heavy_or_frame": True,
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
        )
    )

    assert result.succeeded is True
    assert result.payload["declared_unit_price_usd"] == "8.00"
    assert result.payload["signature_selected"] is False
    assert result.payload["alibaba_submit_calls"] == 0
    assert result.payload["form_fill_elapsed_ms"] >= 0
    assert result.payload["address_source"] == "lingxing_openapi"
    assert observed["target_url"] == target
    assert observed["login_return_url"] == target
    assert observed["login_page_label"] == "阿里下单草稿页"
    assert observed["fill_kwargs"]["customer_order_no"] == PLATFORM_ORDER_NO
    assert observed["fill_kwargs"]["expedited"] is True
    assert observed["fill_kwargs"]["declaration"].purpose == "display"
    assert observed["fill_kwargs"]["facts"].route.name == "Express Expedited"
    assert observed["fill_kwargs"]["facts"].total_weight_kg == Decimal("20")
    assert (
        AlibabaOrderSessionStore(
            tmp_path / "data" / "alibaba_ordering.sqlite3"
        ).get(SYSTEM_ORDER_NO, instance_id="desktop-a")
        is None
    )


def test_prepare_alibaba_order_preserves_safe_capability_error_detail(tmp_path) -> None:
    async def lookup(_settings, _order_identifier):
        raise CapabilityUnavailable(
            "领星 API 订单详情失败（code=1005001, request_id=request-safe-id）。"
        )

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        order_detail_lookup=lookup,
    )

    result = runner(
        TaskCommand(
            "prepare",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_ORDER_PREPARE,
            order_no=PLATFORM_ORDER_NO,
        )
    )

    assert result.succeeded is False
    assert result.blocked is True
    assert "code=1005001" in result.message
    assert "CapabilityUnavailable" not in result.message


def test_prepare_alibaba_order_empty_identifier_mentions_both_supported_numbers(
    tmp_path,
) -> None:
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
    )

    result = runner(
        TaskCommand(
            "prepare",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_ORDER_PREPARE,
            order_no="",
        )
    )

    assert result.succeeded is False
    assert result.blocked is True
    assert result.message == "请输入领星系统单号或平台单号。"


def test_prepare_alibaba_order_does_not_save_session_when_quote_open_fails(
    tmp_path,
    monkeypatch,
) -> None:
    observed: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_context(_endpoint):
        yield object()

    class FakeBrowser:
        def __init__(self, _context):
            pass

        async def draft_urls(self):
            return ()

        async def prepare_quote_page(self, *, login_config):
            assert login_config.auto_login is True
            from shipment_automation.alibaba_ordering import AlibabaOrderRuleError

            raise AlibabaOrderRuleError("阿里查价页打开失败，请检查网络后重试。")

    monkeypatch.setattr(
        "shipment_automation.alibaba_order_browser.attached_alibaba_context",
        fake_context,
    )
    monkeypatch.setattr(
        "shipment_automation.alibaba_order_browser.AlibabaOrderBrowser",
        FakeBrowser,
    )

    async def slow_address_loader(_detail, _context, _system_order_no):
        observed["address_started"] = True
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            observed["address_cancelled"] = True
            raise

    monkeypatch.setattr(
        DesktopTaskRunner,
        "_alibaba_shipping_address",
        staticmethod(slow_address_loader),
    )

    async def lookup(_settings, _order_identifier):
        return _alibaba_order_detail()

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        order_detail_lookup=lookup,
    )
    result = runner(
        TaskCommand(
            "prepare",
            TaskArea.SHIPMENT,
            Capability.ALIBABA_ORDER_PREPARE,
            order_no=SYSTEM_ORDER_NO,
            payload={
                "_desktop_browser_endpoint": "http://127.0.0.1:9222",
                "_desktop_instance_id": "desktop-a",
            },
        )
    )

    assert result.succeeded is False
    assert "查价页打开失败" in result.message
    assert observed == {
        "address_started": True,
        "address_cancelled": True,
    }
    assert (
        AlibabaOrderSessionStore(
            tmp_path / "data" / "alibaba_ordering.sqlite3"
        ).get(SYSTEM_ORDER_NO, instance_id="desktop-a")
        is None
    )


def test_warehouse_failure_is_recorded_against_latest_workflow_stage():
    assert DesktopTaskRunner._paused_custom_stage(
        {
            "instruction_remark_status": "instruction_remark_complete",
            "warehouse_logistics_status": "warehouse_logistics_manual_review",
            "warehouse_logistics_error": "读回不明确",
        }
    ) == "warehouse_logistics"


def test_postal_fallback_diagnostic_is_not_treated_as_a_workflow_error():
    assert DesktopTaskRunner._contains_unresolved_write(
        {
            "status": "updated",
            "warehouse_logistics_status": "warehouse_logistics_complete",
            "warehouse_logistics_complete": True,
            "shipping_postal_source": "detail_dom_fallback",
            "shipping_postal_api_diagnostic": "订单详情接口瞬时失败，已使用页面邮编。",
            "warehouse_logistics_postal_diagnostic": (
                "订单详情接口瞬时失败，页面兜底取得有效五位邮编。"
            ),
        }
    ) is False


def test_amazon_summary_diagnostic_is_not_treated_as_a_finished_stage_error():
    assert DesktopTaskRunner._contains_unresolved_write(
        {
            "status": "updated",
            "contact_write_status": "already_current",
            "folder_status": "folder_created",
            "amazon_order_summary_status": "amazon_order_summary_error",
            "amazon_order_summary_error": "temporary API error",
            "dedupe_final_recorded": True,
        }
    ) is False


def test_real_workflow_stage_error_still_blocks_completion():
    assert DesktopTaskRunner._contains_unresolved_write(
        {
            "status": "updated",
            "folder_status": "folder_created",
            "sku_adjustment_status": "api_error",
            "sku_adjustment_error": "write failed",
        }
    ) is True


def _settings(tmp_path) -> DesktopSettings:
    return DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path=str(tmp_path / "state.sqlite3"),
        queue_path=str(tmp_path / "shipment.sqlite3"),
        browser_profile=str(tmp_path / "browser"),
        log_dir=str(tmp_path / "logs"),
    )


def _seed_shipment_job(
    settings: DesktopSettings,
    logistics_no: str,
    *,
    product_type: str,
    platform_order_no: str = PLATFORM_ORDER_NO,
    system_order_no: str = SYSTEM_ORDER_NO,
) -> ShipmentWorkflowStore:
    store = ShipmentWorkflowStore(settings.queue_path)
    store.upsert_candidate(
        ShipmentCandidate(
            system_order_no=system_order_no,
            platform_order_no=platform_order_no,
            logistics_no=logistics_no,
            shipment_tag_name="自动标发",
            tag_text="自动标发",
            sku_text="test sku",
            product_type=product_type,
            customer_remark=f"重发邮件 {logistics_no}",
            status_text="待审核发货",
        )
    )
    store.complete_logistics_attempt(
        logistics_no,
        LogisticsDetail(
            logistics_no=logistics_no,
            status_text="运输中",
            service_line="UPS-Saver",
            carrier="UPS",
            international_tracking_no="1Z9253126709651051",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_READY,
        last_error=None,
    )
    return store


def _custom_command(
    confirmation: DesktopWriteConfirmation | None = None,
) -> TaskCommand:
    confirmation = confirmation or DesktopWriteConfirmation.create(
        DesktopWriteAction.PROCESS_CUSTOM_ORDER,
        PLATFORM_ORDER_NO,
    )
    return TaskCommand(
        name="process",
        area=TaskArea.CUSTOMIZATION,
        capability=Capability.UPDATE_CONTACT,
        order_no=PLATFORM_ORDER_NO,
        payload={DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload()},
    )


def test_scan_callables_receive_command_execution_id(tmp_path) -> None:
    observed: list[tuple[TaskArea, str | None, str, str]] = []

    def scanner_for(area: TaskArea):
        async def scan(
            _settings: DesktopSettings,
            _configuration: dict[str, Any],
            execution_id: str | None,
            operator_name: str = "",
            operator_email: str = "",
        ) -> dict[str, Any]:
            observed.append((area, execution_id, operator_name, operator_email))
            return {"status": "completed", "message": "scan complete"}

        return scan

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        api_test=scanner_for(TaskArea.MAINTENANCE),
        custom_scan=scanner_for(TaskArea.CUSTOMIZATION),
        shipment_scan=scanner_for(TaskArea.SHIPMENT),
    )

    for area in (
        TaskArea.MAINTENANCE,
        TaskArea.CUSTOMIZATION,
        TaskArea.SHIPMENT,
    ):
        execution_id = f"task-{area.value}"
        result = runner(
            TaskCommand(
                name=f"scan-{area.value}",
                area=area,
                capability=Capability.LIST_ORDERS,
                execution_id=execution_id,
                payload={
                    DESKTOP_OPERATOR_NAME_PAYLOAD_KEY: "Steven",
                    DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY: "steven@billyprint.com",
                },
            )
        )
        assert result.succeeded is True

    assert observed == [
        (TaskArea.MAINTENANCE, "task-maintenance", "", ""),
        (
            TaskArea.CUSTOMIZATION,
            "task-customization",
            "Steven",
            "steven@billyprint.com",
        ),
        (TaskArea.SHIPMENT, "task-shipment", "Steven", "steven@billyprint.com"),
    ]


def test_notification_contact_refresh_is_a_read_only_background_task(tmp_path) -> None:
    observed: list[tuple[str | None, tuple[int, ...]]] = []

    async def refresh(
        _settings: DesktopSettings,
        _configuration: dict[str, Any],
        execution_id: str | None,
        notification_ids: tuple[int, ...],
    ) -> dict[str, Any]:
        observed.append((execution_id, notification_ids))
        return {
            "status": "completed_with_warnings",
            "message": "refreshed",
            "erp_write_calls": 0,
            "external_provider_calls": 0,
        }

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_notification_contact_refresh=refresh,
    )
    result = runner(
        TaskCommand(
            "refresh contacts",
            TaskArea.SHIPMENT,
            Capability.GET_ORDER_DETAIL,
            execution_id="contact-refresh-task",
            payload={
                "trigger": NOTIFICATION_CONTACT_REFRESH_TRIGGER,
                "notification_ids": [2, "2", 3, 0, "bad"],
            },
        )
    )

    assert result.succeeded is True
    assert observed == [("contact-refresh-task", (2, 3))]
    assert result.payload["erp_write_calls"] == 0
    assert result.payload["external_provider_calls"] == 0
    assert result.payload["notification_contact_refresh_duration_ms"] >= 0


def test_notification_review_rescan_uses_dedicated_sync_without_shipment_scan(
    tmp_path,
) -> None:
    calls: list[tuple[str, str | None]] = []

    async def notification_sync(_settings, _configuration, execution_id):
        calls.append(("notification", execution_id))
        return {
            "status": "completed",
            "message": "notification sync complete",
            "alibaba_logistics_query_count": 0,
        }

    async def forbidden_shipment_scan(
        _settings,
        _configuration,
        execution_id,
        _operator_name,
        _operator_email,
    ):
        calls.append(("shipment", execution_id))
        raise AssertionError("notification rescan must not run the shipment scan")

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_scan=forbidden_shipment_scan,
        shipment_notification_sync=notification_sync,
    )
    result = runner(
        TaskCommand(
            name="重新同步客户通知物流",
            area=TaskArea.SHIPMENT,
            capability=Capability.LIST_ORDERS,
            payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
            execution_id="notification-task",
        )
    )

    assert result.succeeded is True
    assert result.payload["alibaba_logistics_query_count"] == 0
    assert result.payload["notification_sync_duration_ms"] >= 0
    assert calls == [("notification", "notification-task")]


def test_notification_recipient_name_conflict_uses_opaque_choice_popup(
    tmp_path,
) -> None:
    observed = {}

    async def notification_sync(_settings, configuration, _execution_id):
        resolver = next(
            value for value in configuration.values() if callable(value)
        )
        observed["selected_name"] = await resolver(
            "112-1234567-1234567",
            ("Customer Alpha", "Customer Beta"),
        )
        return {
            "status": "completed",
            "message": "notification sync complete",
        }

    async def interaction(**kwargs):
        observed["stage"] = kwargs["stage"]
        observed["labels"] = tuple(
            option.label for option in kwargs["options"]
        )
        observed["values"] = tuple(
            option.value for option in kwargs["options"]
        )
        return DesktopInteractionResponse(
            "recipient-choice",
            True,
            selected_value="candidate-2",
        )

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_notification_sync=notification_sync,
        interaction_handler=interaction,
    )
    result = runner(
        TaskCommand(
            name="重新同步客户通知物流",
            area=TaskArea.SHIPMENT,
            capability=Capability.LIST_ORDERS,
            payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
            execution_id="notification-name-task",
        )
    )

    assert result.succeeded is True
    assert observed["stage"] == "notification:recipient_name_select"
    assert observed["labels"] == ("Customer Alpha", "Customer Beta")
    assert observed["values"] == ("candidate-1", "candidate-2")
    assert observed["selected_name"] == "Customer Beta"


def test_custom_order_is_rechecked_and_disposed_when_buyer_requested_cancellation(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    store = CustomWorkflowStore(settings.custom_state_path)
    store.mutate_legacy_record(
        PLATFORM_ORDER_NO,
        lambda _old: {
            "platform_order_no": PLATFORM_ORDER_NO,
            "system_order_no": SYSTEM_ORDER_NO,
            "product_type": "car_magnet",
            "workflow_status": "pending",
        },
        event_type="test_candidate",
        actor="test",
    )
    status_calls: list[tuple[str, str]] = []
    interaction_stages: list[str] = []

    async def status_check(_settings, _configuration, platform_order_no, system_order_no):
        status_calls.append((platform_order_no, system_order_no))
        return SimpleNamespace(
            buyer_cancel_requested=True,
            status_text="买家申请取消",
        )

    async def interaction(**kwargs):
        interaction_stages.append(str(kwargs["stage"]))
        return DesktopInteractionResponse("notice", True)

    async def forbidden_retry(_args):
        raise AssertionError("buyer-cancelled order must not enter the write workflow")

    monkeypatch.setattr(contact_sync, "run_retry_order", forbidden_retry)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        custom_order_status_check=status_check,
        interaction_handler=interaction,
    )
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.PROCESS_CUSTOM_ORDER,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
    )

    result = runner(_custom_command(confirmation))

    assert result.succeeded is True
    assert result.payload["status"] == "not_required"
    assert "买家申请取消" in result.message
    assert status_calls == [(PLATFORM_ORDER_NO, SYSTEM_ORDER_NO)]
    assert interaction_stages == ["buyer_cancelled"]
    workflow = store.get_workflow(PLATFORM_ORDER_NO)
    assert workflow is not None
    assert workflow["workflow_status"] == "not_required"
    assert all(
        row["state"] in {"NOT_REQUIRED", "NOT_APPLICABLE"}
        for row in workflow["stages"]
    )


def test_terminally_cancelled_order_is_disposed_without_interaction(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    store = CustomWorkflowStore(settings.custom_state_path)
    store.mutate_legacy_record(
        PLATFORM_ORDER_NO,
        lambda _old: {
            "platform_order_no": PLATFORM_ORDER_NO,
            "system_order_no": SYSTEM_ORDER_NO,
            "product_type": "car_magnet",
            "workflow_status": "pending",
        },
        event_type="test_candidate",
        actor="test",
    )

    async def status_check(*_args):
        return SimpleNamespace(
            buyer_cancel_requested=False,
            order_cancelled=True,
            status_text="订单已取消",
        )

    async def forbidden_interaction(**_kwargs):
        raise AssertionError("terminal cancellation must not pause for interaction")

    async def forbidden_retry(_args):
        raise AssertionError("cancelled order must not enter the write workflow")

    monkeypatch.setattr(contact_sync, "run_retry_order", forbidden_retry)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        custom_order_status_check=status_check,
        interaction_handler=forbidden_interaction,
    )
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.PROCESS_CUSTOM_ORDER,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
    )

    result = runner(_custom_command(confirmation))

    assert result.succeeded is True
    assert result.payload["status"] == "order_cancelled"
    assert result.payload["order_cancelled"] is True
    assert store.get_workflow(PLATFORM_ORDER_NO)["workflow_status"] == "cancelled"


def test_custom_order_factory_is_created_and_closed_inside_each_task_loop(monkeypatch, tmp_path) -> None:
    events: list[tuple[str, int, object]] = []

    @asynccontextmanager
    async def factory(_settings: DesktopSettings, _configuration: dict[str, Any]):
        operations = object()
        loop_id = id(asyncio.get_running_loop())
        events.append(("enter", loop_id, operations))
        try:
            yield operations
        finally:
            assert id(asyncio.get_running_loop()) == loop_id
            events.append(("exit", loop_id, operations))

    async def fake_retry(args):
        current = events[-1]
        assert current[0] == "enter"
        assert args.custom_order_api_operations is current[2]
        assert args.resume_workflow_stages is True
        assert args.custom_order_interaction_policy is not None
        assert id(asyncio.get_running_loop()) == current[1]
        return {
            "status": "completed",
            "updated_count": 1,
            "message": "ok",
            "items": [{"status": "updated", "message": "ok"}],
        }

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    settings = _settings(tmp_path)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        custom_order_api_factory=factory,
    )
    first = runner(_custom_command())
    second = runner(_custom_command())

    assert first.succeeded is True
    assert second.succeeded is True
    assert [event[0] for event in events] == ["enter", "exit", "enter", "exit"]
    assert events[0][2] is events[1][2]
    assert events[2][2] is events[3][2]
    assert events[0][2] is not events[2][2]


def test_headless_browser_uses_short_login_timeout(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
    )
    args = SimpleNamespace(
        login_timeout_sec=300,
        browser_channel="chrome",
    )
    monkeypatch.setenv("ERP_AUTOMATION_HEADLESS", "1")

    runner._common_browser_args(args, settings)

    assert args.headless is True
    assert args.browser_channel == "bundled"
    assert args.login_timeout_sec == 30


def test_desktop_browser_endpoint_disables_server_headless_mode(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
    )
    args = SimpleNamespace(
        login_timeout_sec=300,
        browser_channel="chrome",
    )
    monkeypatch.setenv("ERP_AUTOMATION_HEADLESS", "1")

    runner._common_browser_args(
        args,
        settings,
        browser_endpoint="http://127.0.0.1:24000",
    )

    assert args.browser_cdp_url == "http://127.0.0.1:24000"
    assert args.headless is False
    assert args.login_timeout_sec == 300


def test_server_headless_alibaba_query_waits_for_local_visible_browser(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
    )
    monkeypatch.setenv("ERP_AUTOMATION_HEADLESS", "1")

    result = asyncio.run(runner._query_logistics(settings, {}))

    assert result.succeeded is False
    assert result.blocked is True
    assert result.payload["status"] == "waiting_for_local_browser"
    assert result.payload["local_visible_browser_required"] is True
    assert result.payload["alibaba_logistics_query_count"] == 0
    assert "本机可见 Chrome" in result.message


def test_server_alibaba_query_uses_supplied_local_visible_browser_endpoint(
    monkeypatch,
    tmp_path,
) -> None:
    from shipment_automation import logistics_worker

    observed: dict[str, Any] = {}

    async def fake_worker(args):
        observed["browser_cdp_url"] = args.browser_cdp_url
        observed["headless"] = args.headless
        return {
            "status": "completed",
            "message": "物流查询完成。",
            "parsed_count": 0,
            "ready_count": 0,
        }

    monkeypatch.setattr(logistics_worker, "run_logistics_worker", fake_worker)
    monkeypatch.setenv("ERP_AUTOMATION_HEADLESS", "1")
    settings = _settings(tmp_path)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
    )

    result = asyncio.run(
        runner._query_logistics(
            settings,
            {},
            browser_endpoint="http://127.0.0.1:24000",
        )
    )

    assert result.succeeded is True
    assert observed == {
        "browser_cdp_url": "http://127.0.0.1:24000",
        "headless": False,
    }


def test_mobile_binding_failure_marks_shared_browser_prerequisite(
    monkeypatch,
    tmp_path,
) -> None:
    from lingxing_automation.browser.session import OrderPageAuthenticationRequired

    async def failed_retry(_args):
        raise OrderPageAuthenticationRequired("服务器浏览器需要完成设备验证。")

    monkeypatch.setattr(contact_sync, "run_retry_order", failed_retry)
    settings = _settings(tmp_path)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
    )

    result = runner(_custom_command())

    assert result.succeeded is False
    assert result.message == "服务器浏览器需要完成设备验证。"
    assert result.payload["shared_prerequisite_error"] == "lingxing_browser_session"
    assert result.payload["browser_session_unavailable"] is True


def test_order_page_load_failure_marks_shared_browser_prerequisite(
    monkeypatch,
    tmp_path,
) -> None:
    from lingxing_automation.browser.session import OrderPageLoadFailed

    async def failed_retry(_args):
        raise OrderPageLoadFailed("服务器浏览器无法加载领星订单页。")

    monkeypatch.setattr(contact_sync, "run_retry_order", failed_retry)
    settings = _settings(tmp_path)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
    )

    result = runner(_custom_command())

    assert result.succeeded is False
    assert result.message == "服务器浏览器无法加载领星订单页。"
    assert result.payload["shared_prerequisite_error"] == "lingxing_browser_session"
    assert result.payload["browser_session_unavailable"] is True


def test_custom_desktop_interactions_never_read_stdin_and_guard_is_dynamic(
    monkeypatch,
    tmp_path,
) -> None:
    guard_state = {"enabled": True}

    def forbidden_input(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("packaged desktop task must never read stdin")

    monkeypatch.setattr(builtins, "input", forbidden_input)

    async def fake_retry(args):
        policy = args.custom_order_interaction_policy
        assert await policy.confirm_writeback(
            {
                "expected_platform_order_no": PLATFORM_ORDER_NO,
                "expected_system_order_no": SYSTEM_ORDER_NO,
            }
        )
        assert await policy.confirm_folder_creation(PLATFORM_ORDER_NO, SYSTEM_ORDER_NO, object())
        plan = SimpleNamespace(platform_order_no=PLATFORM_ORDER_NO)
        assert await policy.confirm_sku_plan(plan)
        assert await policy.confirm_package_split_plan(plan)
        assert not await policy.confirm_manual_sku_done(PLATFORM_ORDER_NO, SYSTEM_ORDER_NO, "manual")
        assert not await policy.confirm_manual_package_split_done(plan)
        contact = ContactInfo(
            phone="15551234567",
            email="buyer@example.com",
            source_count=1,
            source_excerpt="test",
        )
        assert await policy.choose_contact(PLATFORM_ORDER_NO, SYSTEM_ORDER_NO, [contact]) is contact
        assert await policy.runtime_write_guard("contact_email", PLATFORM_ORDER_NO, SYSTEM_ORDER_NO)
        guard_state["enabled"] = False
        assert not await policy.runtime_write_guard("folder_create", PLATFORM_ORDER_NO, SYSTEM_ORDER_NO)
        return {
            "status": "completed",
            "updated_count": 1,
            "items": [{"status": "updated"}],
        }

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    async def interaction_handler(**request: Any) -> DesktopInteractionResponse:
        accepted = not str(request.get("stage") or "").startswith("manual_")
        return DesktopInteractionResponse("test-response", accepted)

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: guard_state["enabled"],
        interaction_handler=interaction_handler,
    )

    result = runner(_custom_command())

    assert result.succeeded is True
    assert result.payload["desktop_confirmed_steps"][-2:] == [
        "write_guard_allowed:contact_email",
        "write_guard_blocked:folder_create",
    ]


def test_single_incomplete_contact_auto_approves_routine_contact_and_folder_steps(
    monkeypatch,
    tmp_path,
) -> None:
    requests: list[dict[str, Any]] = []

    async def fake_retry(args):
        policy = args.custom_order_interaction_policy
        contact = ContactInfo(
            phone="5551234567",
            email=None,
            source_count=1,
            source_excerpt="phone only",
        )
        selected = await policy.choose_contact(
            PLATFORM_ORDER_NO,
            SYSTEM_ORDER_NO,
            [contact],
        )
        assert selected is contact
        assert await policy.confirm_writeback(
            {
                "expected_platform_order_no": PLATFORM_ORDER_NO,
                "expected_system_order_no": SYSTEM_ORDER_NO,
                "current_identity": {"system_order_no": SYSTEM_ORDER_NO},
                "before_values": {"phone": "-", "email": "-"},
                "after_fill_values": {"phone": contact.phone, "email": None},
            }
        )
        folder_result = FolderBuildResult(
            status="folder_preview",
            folder_name=f"{PLATFORM_ORDER_NO}+1个测试产品+Buyer+直接制作",
            folder_path=str(tmp_path / "orders" / PLATFORM_ORDER_NO),
            folder_components=[
                PLATFORM_ORDER_NO,
                "1个测试产品",
                "Buyer",
                "直接制作",
            ],
        )
        assert await policy.confirm_folder_creation(
            PLATFORM_ORDER_NO,
            SYSTEM_ORDER_NO,
            folder_result,
        )
        return {
            "status": "completed",
            "updated_count": 1,
            "items": [{"status": "updated"}],
        }

    async def interaction_handler(**request: Any) -> DesktopInteractionResponse:
        requests.append(dict(request))
        return DesktopInteractionResponse("test-response", True)

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        interaction_handler=interaction_handler,
    )

    result = runner(_custom_command())

    assert result.succeeded is True
    assert requests == []
    assert "contact_writeback_auto_approved" in result.payload[
        "desktop_confirmed_steps"
    ]
    assert "folder_creation_auto_approved" in result.payload[
        "desktop_confirmed_steps"
    ]


def test_folder_confirmation_warns_when_name_was_shortened() -> None:
    removed_package = (
        "1个(3x6m帐篷顶+相同设计+40mm方形铝+1全高背墙+400D面料+"
        "拖轮包+沙袋六件套+绳子地钉)"
    )
    full_name = f"{PLATFORM_ORDER_NO}+1个(3x3m帐篷顶)+{removed_package}+Farrah Ferris"
    safe_name = f"{PLATFORM_ORDER_NO}+1个(3x3m帐篷顶)+Farrah Ferris"
    result = FolderBuildResult(
        status="folder_preview",
        folder_name=safe_name,
        folder_name_full=full_name,
        folder_path=f"Z:/Amazon/{safe_name}",
        folder_components=[PLATFORM_ORDER_NO, "1个(3x3m帐篷顶)", "Farrah Ferris"],
        folder_components_full=[
            PLATFORM_ORDER_NO,
            "1个(3x3m帐篷顶)",
            removed_package,
            "Farrah Ferris",
        ],
        folder_name_removed_components=[removed_package],
        folder_name_was_shortened=True,
        folder_name_max_length=180,
    )

    message = DesktopTaskRunner._folder_confirmation_message(
        PLATFORM_ORDER_NO,
        SYSTEM_ORDER_NO,
        result,
    )

    assert f"完整文件夹名：{full_name}\n\n实际文件夹名：{safe_name}" in message
    assert "【重要提示" not in message
    assert "从实际目录名中删除的组件" not in message
    assert "完整文件夹名.txt" not in message
    assert "文件夹状态：folder_preview" in message
    assert "完整路径：" in message
    assert "组件：" in message
    assert "警告：-" in message


def test_sku_plan_confirmation_is_a_concise_chinese_summary() -> None:
    plan = TentSkuAdjustmentPlan(
        platform_order_no=PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        destination=DestinationRegion(
            raw_text="United States, CA",
            country="US",
            state="CA",
            category="us_mainland",
        ),
        replace_main_items=[
            TentSkuPlanAction(
                action="replace_main",
                source_sku="custom-tent-package-10x10",
                sku="TENT-ROLLER-BAG-10X10-50MM",
                quantity=1,
            )
        ],
        add_items=[
            TentSkuPlanAction(
                action="add_product",
                sku="SANDBAGS-4PCS",
                quantity=1,
                reason="沙袋四件套",
            )
        ],
        customer_remark="请附说明书",
    )

    message = DesktopTaskRunner._format_sku_plan_for_user(plan)

    assert message == (
        f"平台单号：{PLATFORM_ORDER_NO}\n"
        f"系统单号：{SYSTEM_ORDER_NO}\n"
        "收货地区：美国本土（US / CA）\n\n"
        "替换商品：\n"
        "  1. custom-tent-package-10x10 → TENT-ROLLER-BAG-10X10-50MM × 1\n\n"
        "新增商品：\n"
        "  1. SANDBAGS-4PCS × 1（沙袋四件套）"
    )
    assert "sku_adjustment_" not in message
    assert '"' not in message
    assert "null" not in message


def test_3x6m_roller_review_matches_corrected_execution_plan() -> None:
    plan = build_tent_sku_plan(
        platform_order_no="112-7981230-6009815",
        system_order_no="103722794490200604",
        folder_components=[
            "112-7981230-6009815",
            "1个3x6m帐篷顶",
            "拖轮包",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), OK, Bixby ZIP 74008",
        order_lines=[
            OrderFolderLine(
                asin="B0F5CKNVYJ",
                sku="Canopy-Tent-10x20",
                parent_asin="B0F5CTQXG1",
                product_type="tent",
                quantity=1,
                customization_text="",
                order_item_id="affected-order-row",
            )
        ],
    )

    message = DesktopTaskRunner._format_sku_plan_for_user(plan)

    assert "Canopy-Tent-10x20 → TENT-ROLLER-BAG-10X20-50MM × 1" in message
    assert "新增商品：\n  1. TENT-ROLLER-BAG-10X20-50MM" not in message
    assert sum(item.quantity for item in plan.replace_main_items if item.sku == "TENT-ROLLER-BAG-10X20-50MM") == 1
    assert sum(item.quantity for item in plan.add_items if item.sku == "TENT-ROLLER-BAG-10X20-50MM") == 0


def test_package_split_confirmation_is_a_concise_chinese_summary() -> None:
    plan = TentPackageSplitPlan(
        platform_order_no=PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        destination=DestinationRegion(raw_text="United States, CA"),
        status="package_split_required",
        required=True,
        reason="支架需要单独打包",
        customer_remark="7.23发说明书",
        packages_to_split=[
            TentPackageSplitPackage(
                package_key="frame-1",
                title="支架包1",
                items=[
                    TentPackageSplitItem(
                        sku="10X10-FRAME-40MM-HEX",
                        quantity=1,
                        reason="40mm六角铝",
                    )
                ],
            ),
            TentPackageSplitPackage(
                package_key="frame-2",
                title="支架包2",
                items=[
                    TentPackageSplitItem(
                        sku="10X20-FRAME-40MM-SQUARE",
                        quantity=1,
                        reason="40mm方形铝",
                    )
                ],
            ),
        ],
    )

    message = DesktopTaskRunner._format_package_split_plan_for_user(plan)

    assert message == (
        f"平台单号：{PLATFORM_ORDER_NO}\n"
        f"系统单号：{SYSTEM_ORDER_NO}\n"
        "拆包原因：支架需要单独打包\n\n"
        "将拆出 2 个新包裹：\n"
        "  1. 支架包1\n"
        "     • 10X10-FRAME-40MM-HEX × 1（40mm六角铝）\n"
        "  2. 支架包2\n"
        "     • 10X20-FRAME-40MM-SQUARE × 1（40mm方形铝）\n\n"
        "其余商品保留在原包裹中。\n\n"
        "说明书客服备注：7.23发说明书"
    )
    assert "package_key" not in message
    assert "package_split_required" not in message
    assert '"' not in message


def test_write_confirmation_is_required_and_cannot_be_reused(monkeypatch, tmp_path) -> None:
    calls = 0

    async def fake_retry(_args):
        nonlocal calls
        calls += 1
        return {
            "status": "completed",
            "updated_count": 1,
            "items": [{"status": "updated"}],
        }

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: True,
    )
    missing = TaskCommand(
        name="process",
        area=TaskArea.CUSTOMIZATION,
        capability=Capability.UPDATE_CONTACT,
        order_no=PLATFORM_ORDER_NO,
    )
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.PROCESS_CUSTOM_ORDER,
        PLATFORM_ORDER_NO,
    )
    command = _custom_command(confirmation)

    assert runner(missing).blocked is True
    assert runner(command).succeeded is True
    replay = runner(command)

    assert replay.blocked is True
    assert "已经使用" in replay.message
    assert calls == 1


def test_nested_ambiguous_result_stays_pending_with_retry_review_lock(monkeypatch, tmp_path) -> None:
    async def fake_retry(_args):
        return {
            "status": "completed",
            "updated_count": 1,
            "items": [
                {
                    "status": "updated_folder_created_sku_failed",
                    "sku_adjustment_status": "sku_adjustment_manual_review",
                    "sku_adjustment_error": "API 结果不明确，禁止重复写入。",
                    "message": "API 结果不明确，禁止重复写入。",
                }
            ],
        }

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    settings = _settings(tmp_path)
    store = CustomWorkflowStore(settings.custom_state_path)
    store.mutate_legacy_record(
        PLATFORM_ORDER_NO,
        lambda _current: {
            "platform_order_no": PLATFORM_ORDER_NO,
            "contact_writeback_complete": True,
            "folder_complete": True,
            "sku_adjustment_required": True,
            "sku_adjustment_complete": False,
            "workflow_status": "sku_adjustment_pending",
        },
        event_type="test_initialized",
        actor="test",
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: True,
    )

    result = runner(_custom_command())

    assert result.succeeded is False
    assert result.blocked is True
    assert result.payload["status"] == "manual_review"
    assert result.payload["workflow_paused_stage"] == "sku"
    assert result.payload["workflow_pause_recorded"] is True
    workflow = CustomWorkflowStore(settings.custom_state_path).get_workflow(PLATFORM_ORDER_NO)
    assert workflow is not None
    sku = next(stage for stage in workflow["stages"] if stage["stage"] == "sku")
    assert workflow["workflow_status"] == "sku_adjustment_pending"
    assert sku["state"] == str(WorkflowStageState.PENDING)
    assert sku["last_error"] == "API 结果不明确，禁止重复写入。"
    assert CustomWorkflowStore(settings.custom_state_path).get_pending_retry_review(
        PLATFORM_ORDER_NO
    )["stage"] == "sku"


def test_retry_review_close_keeps_lock_and_never_repeats_write(monkeypatch, tmp_path) -> None:
    calls = 0

    async def fake_retry(_args):
        nonlocal calls
        calls += 1
        return {"status": "completed", "updated_count": 1, "items": [{"status": "updated"}]}

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    settings = _settings(tmp_path)
    store = CustomWorkflowStore(settings.custom_state_path)
    store.mutate_legacy_record(
        PLATFORM_ORDER_NO,
        lambda _current: {
            "platform_order_no": PLATFORM_ORDER_NO,
            "contact_writeback_complete": True,
            "folder_complete": True,
            "sku_adjustment_required": True,
            "sku_adjustment_complete": False,
        },
        event_type="test_initialized",
        actor="test",
    )
    store.record_workflow_paused(
        PLATFORM_ORDER_NO,
        "sku",
        reason="写入结果无法确认",
        result_status="unknown",
        pause_kind=WorkflowPauseKind.AMBIGUOUS_WRITE,
    )

    async def interaction_handler(**_request: Any) -> DesktopInteractionResponse:
        return DesktopInteractionResponse("review", False)

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: True,
        interaction_handler=interaction_handler,
    )
    result = runner(_custom_command())

    assert result.cancelled is True
    assert result.payload["retry_review_required"] is True
    assert calls == 0
    assert store.get_pending_retry_review(PLATFORM_ORDER_NO)["stage"] == "sku"


def test_retry_review_can_mark_verified_stage_complete_without_repeating_write(
    monkeypatch, tmp_path
) -> None:
    calls = 0

    async def fake_retry(_args):
        nonlocal calls
        calls += 1
        return {"status": "completed", "updated_count": 1, "items": [{"status": "updated"}]}

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    settings = _settings(tmp_path)
    store = CustomWorkflowStore(settings.custom_state_path)
    store.mutate_legacy_record(
        PLATFORM_ORDER_NO,
        lambda _current: {
            "platform_order_no": PLATFORM_ORDER_NO,
            "contact_writeback_complete": True,
            "folder_complete": True,
            "sku_adjustment_required": True,
            "sku_adjustment_complete": False,
            "package_split_required": False,
            "instruction_remark_required": False,
        },
        event_type="test_initialized",
        actor="test",
    )
    store.record_workflow_paused(
        PLATFORM_ORDER_NO,
        "sku",
        reason="写入结果无法确认",
        result_status="unknown",
        pause_kind=WorkflowPauseKind.AMBIGUOUS_WRITE,
    )

    async def interaction_handler(**request: Any) -> DesktopInteractionResponse:
        assert request["stage"] == "retry_review:sku"
        return DesktopInteractionResponse(
            "review",
            True,
            str(StageRetryReviewResolution.COMPLETED),
        )

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: True,
        interaction_handler=interaction_handler,
    )
    result = runner(_custom_command())

    assert result.succeeded is True
    assert calls == 0
    assert store.get_workflow(PLATFORM_ORDER_NO)["workflow_status"] == "completed"


def test_retry_review_confirmed_not_executed_unlocks_and_runs_stage(monkeypatch, tmp_path) -> None:
    calls = 0

    async def fake_retry(_args):
        nonlocal calls
        calls += 1
        return {"status": "completed", "updated_count": 1, "items": [{"status": "updated"}]}

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    settings = _settings(tmp_path)
    store = CustomWorkflowStore(settings.custom_state_path)
    store.mutate_legacy_record(
        PLATFORM_ORDER_NO,
        lambda _current: {
            "platform_order_no": PLATFORM_ORDER_NO,
            "contact_writeback_complete": True,
            "folder_complete": True,
            "sku_adjustment_required": True,
            "sku_adjustment_complete": False,
        },
        event_type="test_initialized",
        actor="test",
    )
    store.record_workflow_paused(
        PLATFORM_ORDER_NO,
        "sku",
        reason="写入结果无法确认",
        result_status="unknown",
        pause_kind=WorkflowPauseKind.AMBIGUOUS_WRITE,
    )

    async def interaction_handler(**_request: Any) -> DesktopInteractionResponse:
        return DesktopInteractionResponse(
            "review",
            True,
            str(StageRetryReviewResolution.RETRY),
        )

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: True,
        interaction_handler=interaction_handler,
    )
    result = runner(_custom_command())

    assert result.succeeded is True
    assert calls == 1
    assert store.get_pending_retry_review(PLATFORM_ORDER_NO) is None


def test_emergency_stop_pauses_current_stage_without_review_lock(monkeypatch, tmp_path) -> None:
    async def fake_retry(args):
        allowed = await args.custom_order_interaction_policy.runtime_write_guard(
            "folder_create",
            PLATFORM_ORDER_NO,
            "",
        )
        assert allowed is False
        return {
            "status": "completed",
            "updated_count": 0,
            "items": [
                {
                    "status": "folder_write_blocked",
                    "folder_status": "paused_by_emergency_stop",
                    "folder_error": "运行时急停，文件夹尚未创建。",
                    "message": "运行时急停，文件夹尚未创建。",
                }
            ],
        }

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    settings = _settings(tmp_path)
    store = CustomWorkflowStore(settings.custom_state_path)
    store.mutate_legacy_record(
        PLATFORM_ORDER_NO,
        lambda _current: {
            "platform_order_no": PLATFORM_ORDER_NO,
            "contact_writeback_complete": True,
            "folder_complete": False,
        },
        event_type="test_initialized",
        actor="test",
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: False,
    )

    result = runner(_custom_command())

    assert result.cancelled is True
    assert result.blocked is False
    workflow = store.get_workflow(PLATFORM_ORDER_NO)
    stages = {stage["stage"]: stage for stage in workflow["stages"]}
    assert stages["contact"]["state"] == "COMPLETED"
    assert stages["folder"]["state"] == "PENDING"
    assert workflow["workflow_status"] == "folder_pending"
    assert store.get_pending_retry_review(PLATFORM_ORDER_NO) is None


def test_definite_failure_keeps_current_stage_retryable_and_preserves_prior_completion(
    monkeypatch, tmp_path
) -> None:
    async def fake_retry(_args):
        return {
            "status": "completed",
            "updated_count": 0,
            "items": [
                {
                    "status": "folder_failed",
                    "folder_status": "folder_api_failed",
                    "folder_error": "API 明确拒绝，尚未创建文件夹。",
                    "message": "API 明确拒绝，尚未创建文件夹。",
                }
            ],
        }

    monkeypatch.setattr(contact_sync, "run_retry_order", fake_retry)
    settings = _settings(tmp_path)
    store = CustomWorkflowStore(settings.custom_state_path)
    store.mutate_legacy_record(
        PLATFORM_ORDER_NO,
        lambda _current: {
            "platform_order_no": PLATFORM_ORDER_NO,
            "contact_writeback_complete": True,
            "folder_complete": False,
        },
        event_type="test_initialized",
        actor="test",
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        runtime_write_guard_provider=lambda: True,
    )

    result = runner(_custom_command())

    assert result.succeeded is False
    assert result.blocked is False
    assert result.cancelled is False
    workflow = store.get_workflow(PLATFORM_ORDER_NO)
    stages = {stage["stage"]: stage for stage in workflow["stages"]}
    assert stages["contact"]["state"] == "COMPLETED"
    assert stages["folder"]["state"] == "PENDING"
    assert workflow["workflow_status"] == "folder_pending"


def test_erp_desktop_confirmation_callback_never_reads_stdin(monkeypatch, tmp_path) -> None:
    def forbidden_input(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("packaged desktop ERP worker must never read stdin")

    monkeypatch.setattr(builtins, "input", forbidden_input)
    observed: dict[str, Any] = {}
    interaction_requests: list[dict[str, Any]] = []

    async def fake_worker(args):
        observed["confirmation_id"] = args.confirm_func.confirmation_id
        observed["source"] = args.confirm_func.confirmation_source
        assert await args.confirm_func("dangerous write one") is True
        assert await args.confirm_func(
            "即将执行【审核运单填写信息】\n"
            "waybill_no：1Z999\ntracking_no：ALS001\n"
            "pkg_fee_weight：12500\npkg_fee_weight_unit：g"
        ) is True
        return {"status": "completed", "message": "ok"}

    monkeypatch.setattr(erp_mark_ship, "run_erp_mark_worker", fake_worker)
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.EXECUTE_ERP_MARK,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        logistics_no="LP123456789",
    )
    command = TaskCommand(
        name="mark",
        area=TaskArea.SHIPMENT,
        capability=Capability.OUTBOUND_ORDER,
        order_no=PLATFORM_ORDER_NO,
        payload={
            "system_order_no": SYSTEM_ORDER_NO,
            "logistics_no": "LP123456789",
            DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
        },
    )
    async def interaction_handler(**_request: Any) -> DesktopInteractionResponse:
        interaction_requests.append(dict(_request))
        return DesktopInteractionResponse("test-response", True)

    settings = _settings(tmp_path)
    _seed_shipment_job(settings, "LP123456789", product_type="tent")
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        interaction_handler=interaction_handler,
    )

    result = runner(command)

    assert result.succeeded is True
    assert observed == {
        "confirmation_id": confirmation.confirmation_id,
        "source": "qt_message_box",
    }
    assert len(result.payload["desktop_confirmed_prompt_hashes"]) == 2
    assert len(result.payload["desktop_auto_approved_prompt_hashes"]) == 2
    assert result.payload["desktop_user_confirmed_prompt_hashes"] == []
    assert interaction_requests == []


def test_erp_routine_stage_uses_checked_action_without_opening_interaction(
    monkeypatch,
    tmp_path,
) -> None:
    async def fake_worker(args):
        assert await args.confirm_func("即将通过领星 API 执行【设置仓库物流】。") is True
        assert args.confirm_func.confirmation_source == "qt_checked_action"
        return {"status": "completed", "message": "ok"}

    monkeypatch.setattr(erp_mark_ship, "run_erp_mark_worker", fake_worker)
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.EXECUTE_ERP_MARK,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        logistics_no="ALS-ROUTINE",
        source="qt_checked_action",
    )
    command = TaskCommand(
        "execute",
        TaskArea.SHIPMENT,
        Capability.OUTBOUND_ORDER,
        order_no=PLATFORM_ORDER_NO,
        payload={
            "system_order_no": SYSTEM_ORDER_NO,
            "logistics_no": "ALS-ROUTINE",
            DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
        },
    )
    settings = _settings(tmp_path)
    _seed_shipment_job(settings, "ALS-ROUTINE", product_type=" TENT ")
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        interaction_handler=lambda **_request: pytest.fail(
            "常规标发阶段不应创建桌面交互"
        ),
    )

    result = runner(command)

    assert result.succeeded is True
    assert len(result.payload["desktop_auto_approved_prompt_hashes"]) == 1
    assert result.payload["desktop_user_confirmed_prompt_hashes"] == []


@pytest.mark.parametrize(
    ("product_type", "expected_label"),
    [
        ("tablecloths", "tablecloths"),
        ("vinyl_banners", "vinyl_banners"),
        ("", "未识别"),
    ],
)
def test_non_tent_and_unknown_shipments_require_each_desktop_stage_review(
    monkeypatch,
    tmp_path,
    product_type,
    expected_label,
) -> None:
    prompts = [
        "即将执行【设置仓库物流】",
        "即将执行【审核发货】",
        "即将执行【审核运单填写信息】",
        "即将执行【出库发货】",
        "即将执行【审核快速出库运单信息】",
        "领星 API【审核发货】已明确拒绝，是否改用原网页流程？",
    ]

    async def fake_worker(args):
        for prompt in prompts:
            assert await args.confirm_func(prompt) is True
        return {"status": "completed", "message": "ok", "done_count": 1}

    monkeypatch.setattr(erp_mark_ship, "run_erp_mark_worker", fake_worker)
    settings = _settings(tmp_path)
    _seed_shipment_job(
        settings,
        "ALS-STAGE-REVIEW",
        product_type=product_type,
    )
    requests: list[dict[str, Any]] = []

    async def interaction_handler(**request: Any) -> DesktopInteractionResponse:
        requests.append(dict(request))
        return DesktopInteractionResponse(f"review-{len(requests)}", True)

    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.EXECUTE_ERP_MARK,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        logistics_no="ALS-STAGE-REVIEW",
        source="qt_checked_action",
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        interaction_handler=interaction_handler,
    )

    result = runner(
        TaskCommand(
            "execute",
            TaskArea.SHIPMENT,
            Capability.OUTBOUND_ORDER,
            order_no=PLATFORM_ORDER_NO,
            payload={
                "system_order_no": SYSTEM_ORDER_NO,
                "logistics_no": "ALS-STAGE-REVIEW",
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
        )
    )

    assert result.succeeded is True
    assert len(requests) == len(prompts)
    assert [request["title"].split("：", 1)[-1] for request in requests] == [
        "设置仓库物流",
        "审核发货",
        "审核运单填写信息",
        "出库发货",
        "审核快速出库运单信息",
        "API 失败后改用网页流程",
    ]
    assert requests[-1]["stage"] == "erp_mark:browser_fallback"
    for request in requests:
        message = request["message"]
        assert f"商品类型：{expected_label}" in message
        assert f"系统单号：{SYSTEM_ORDER_NO}" in message
        assert f"平台单号：{PLATFORM_ORDER_NO}" in message
        assert "承运商：UPS" in message
        assert "国际物流单号：1Z9253126709651051" in message
        assert "仓库 / 物流渠道：" in message
        assert "运费：CNY 123.45" in message
        assert "计费重量：4.500 kg" in message
    expected_hashes = [
        hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        for prompt in prompts
    ]
    assert result.payload["desktop_confirmed_prompt_hashes"] == expected_hashes
    assert result.payload["desktop_auto_approved_prompt_hashes"] == []
    assert result.payload["desktop_user_confirmed_prompt_hashes"] == expected_hashes


def test_mixed_shipment_batch_auto_approves_only_tent_orders(
    monkeypatch,
    tmp_path,
) -> None:
    async def fake_worker(args):
        assert await args.confirm_func("即将执行【审核发货】") is True
        return {"status": "completed", "message": "ok", "done_count": 1}

    monkeypatch.setattr(erp_mark_ship, "run_erp_mark_worker", fake_worker)
    settings = _settings(tmp_path)
    orders = (
        ("111-TENT", "SYS-TENT", "ALS-TENT", "tent"),
        ("111-CLOTH", "SYS-CLOTH", "ALS-CLOTH", "tablecloths"),
    )
    for platform_no, system_no, logistics_no, product_type in orders:
        _seed_shipment_job(
            settings,
            logistics_no,
            product_type=product_type,
            platform_order_no=platform_no,
            system_order_no=system_no,
        )
    requests: list[dict[str, Any]] = []

    async def interaction_handler(**request: Any) -> DesktopInteractionResponse:
        requests.append(dict(request))
        return DesktopInteractionResponse("approved", True)

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        interaction_handler=interaction_handler,
    )
    results = []
    for platform_no, system_no, logistics_no, _product_type in orders:
        confirmation = DesktopWriteConfirmation.create(
            DesktopWriteAction.EXECUTE_ERP_MARK,
            platform_no,
            system_order_no=system_no,
            logistics_no=logistics_no,
            source="qt_checked_action",
        )
        results.append(
            runner(
                TaskCommand(
                    "execute",
                    TaskArea.SHIPMENT,
                    Capability.OUTBOUND_ORDER,
                    order_no=platform_no,
                    payload={
                        "system_order_no": system_no,
                        "logistics_no": logistics_no,
                        DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
                    },
                )
            )
        )

    assert results[0].payload["desktop_auto_approved_prompt_hashes"]
    assert results[0].payload["desktop_user_confirmed_prompt_hashes"] == []
    assert results[1].payload["desktop_auto_approved_prompt_hashes"] == []
    assert results[1].payload["desktop_user_confirmed_prompt_hashes"]
    assert len(requests) == 1
    assert "商品类型：tablecloths" in requests[0]["message"]


def test_rejecting_non_tent_stage_stops_later_stage_confirmations(
    monkeypatch,
    tmp_path,
) -> None:
    reached: list[str] = []

    async def fake_worker(args):
        reached.append("设置仓库物流")
        assert await args.confirm_func("即将执行【设置仓库物流】") is True
        reached.append("审核发货")
        if not await args.confirm_func("即将执行【审核发货】"):
            return {
                "status": "completed_with_skips",
                "message": "用户拒绝",
                "done_count": 0,
                "skipped_count": 1,
            }
        reached.append("审核运单填写信息")
        pytest.fail("拒绝审核发货后不得继续填写运单")

    monkeypatch.setattr(erp_mark_ship, "run_erp_mark_worker", fake_worker)
    settings = _settings(tmp_path)
    _seed_shipment_job(
        settings,
        "ALS-REJECT",
        product_type="vinyl_banners",
    )
    requests: list[dict[str, Any]] = []

    async def interaction_handler(**request: Any) -> DesktopInteractionResponse:
        requests.append(dict(request))
        return DesktopInteractionResponse(
            f"review-{len(requests)}",
            len(requests) == 1,
        )

    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.EXECUTE_ERP_MARK,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        logistics_no="ALS-REJECT",
        source="qt_checked_action",
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: settings,
        configuration_provider=lambda: {},
        interaction_handler=interaction_handler,
    )

    result = runner(
        TaskCommand(
            "execute",
            TaskArea.SHIPMENT,
            Capability.OUTBOUND_ORDER,
            order_no=PLATFORM_ORDER_NO,
            payload={
                "system_order_no": SYSTEM_ORDER_NO,
                "logistics_no": "ALS-REJECT",
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
        )
    )

    assert result.succeeded is True
    assert reached == ["设置仓库物流", "审核发货"]
    assert len(requests) == 2
    assert len(result.payload["desktop_confirmed_prompt_hashes"]) == 1
    assert result.payload["desktop_auto_approved_prompt_hashes"] == []
    assert len(result.payload["desktop_user_confirmed_prompt_hashes"]) == 1


def test_api_erp_mark_does_not_launch_browser_when_fallback_is_unused(
    monkeypatch,
    tmp_path,
) -> None:
    from lingxing_automation.browser import session as browser_session

    async def forbidden_launch(*_args, **_kwargs):
        raise AssertionError("API 自动标发成功时不应启动 Chrome")

    async def managed_mark(
        page,
        _item,
        _confirm,
        _checkpoint=None,
        _approval=None,
        _runtime_guard=None,
        *,
        browser_page_provider=None,
    ):
        assert page is None
        assert callable(browser_page_provider)
        return "API_OUTBOUNDED"

    managed_mark.supports_lazy_browser_fallback = True  # type: ignore[attr-defined]
    managed_mark.requires_browser_fallback = False  # type: ignore[attr-defined]

    async def fake_worker(args):
        async def confirm(_prompt):
            return True

        assert await args.mark_item_func(None, object(), confirm) == "API_OUTBOUNDED"
        return {
            "status": "completed",
            "message": "API 标发完成",
            "done_count": 1,
        }

    monkeypatch.setattr(browser_session, "launch_context", forbidden_launch)
    monkeypatch.setattr(erp_mark_ship, "run_erp_mark_worker", fake_worker)
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.EXECUTE_ERP_MARK,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        logistics_no="ALS-API-ONLY",
        source="qt_checked_action",
    )
    progress: list[tuple[str, str, int]] = []
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        erp_mark_func=managed_mark,
        progress_handler=lambda task_id, message, percent: progress.append(
            (task_id, message, percent)
        ),
    )

    result = runner(
        TaskCommand(
            "execute",
            TaskArea.SHIPMENT,
            Capability.OUTBOUND_ORDER,
            order_no=PLATFORM_ORDER_NO,
            execution_id="api-mark-task",
            payload={
                "system_order_no": SYSTEM_ORDER_NO,
                "logistics_no": "ALS-API-ONLY",
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
        )
    )

    assert result.succeeded is True
    assert result.message == "API 标发完成"
    assert result.payload["erp_mark_browser_fallback_used"] is False
    assert result.payload["erp_mark_duration_ms"] >= 0
    assert progress[0] == (
        "api-mark-task",
        "正在读取自动标发队列并准备领星 API。",
        20,
    )


def test_completed_erp_mark_runs_targeted_notification_sync_without_sending(
    monkeypatch,
    tmp_path,
) -> None:
    async def fake_worker(_args):
        return {"status": "completed", "message": "marked", "done_count": 1}

    sync_calls: list[tuple[str | None, tuple[str, ...] | None]] = []

    async def targeted_sync(_settings, _configuration, execution_id, platforms):
        sync_calls.append((execution_id, platforms))
        return {
            "status": "completed",
            "notification_sync": {"notification_count": 1},
            "external_provider_calls": 0,
        }

    monkeypatch.setattr(erp_mark_ship, "run_erp_mark_worker", fake_worker)
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.EXECUTE_ERP_MARK,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        logistics_no="ALS-AUTO-SYNC",
        source="qt_checked_action",
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_notification_sync=targeted_sync,
    )

    result = runner(
        TaskCommand(
            "execute",
            TaskArea.SHIPMENT,
            Capability.OUTBOUND_ORDER,
            order_no=PLATFORM_ORDER_NO,
            execution_id="mark-task",
            payload={
                "system_order_no": SYSTEM_ORDER_NO,
                "logistics_no": "ALS-AUTO-SYNC",
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
        )
    )

    assert result.succeeded is True
    assert sync_calls == [("mark-task", (PLATFORM_ORDER_NO,))]
    assert result.payload["notification_sync"] == {"notification_count": 1}
    assert result.payload["notification_sync_external_provider_calls"] == 0


def test_erp_mark_never_sends_customer_notification_even_with_legacy_flag(
    monkeypatch,
    tmp_path,
) -> None:
    async def fake_worker(_args):
        return {"status": "completed", "message": "marked", "done_count": 1}

    calls: list[tuple[str, tuple[str, ...]]] = []

    async def targeted_sync(_settings, _configuration, execution_id, platforms):
        calls.append(("sync", tuple(platforms or ())))
        return {
            "status": "completed",
            "notification_sync": {"notification_count": 1},
            "external_provider_calls": 0,
        }

    monkeypatch.setattr(erp_mark_ship, "run_erp_mark_worker", fake_worker)
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.EXECUTE_ERP_MARK,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        logistics_no="ALS-CONFIRMED-SEND",
        source="qt_checked_action",
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_notification_sync=targeted_sync,
    )

    result = runner(
        TaskCommand(
            "confirm and execute",
            TaskArea.SHIPMENT,
            Capability.OUTBOUND_ORDER,
            order_no=PLATFORM_ORDER_NO,
            execution_id="confirmed-mark-task",
            payload={
                "system_order_no": SYSTEM_ORDER_NO,
                "logistics_no": "ALS-CONFIRMED-SEND",
                "auto_send_customer_notification": True,
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
        )
    )

    assert result.succeeded is True
    assert calls == [("sync", (PLATFORM_ORDER_NO,))]
    assert "customer_notification_send" not in result.payload
    assert "customer_notification_send_warning" not in result.payload


def test_notification_sync_failure_does_not_rollback_completed_erp_mark(
    monkeypatch,
    tmp_path,
) -> None:
    async def fake_worker(_args):
        return {"status": "completed", "message": "marked", "done_count": 1}

    async def failed_sync(*_args):
        raise RuntimeError("temporary WMS failure")

    monkeypatch.setattr(erp_mark_ship, "run_erp_mark_worker", fake_worker)
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.EXECUTE_ERP_MARK,
        PLATFORM_ORDER_NO,
        system_order_no=SYSTEM_ORDER_NO,
        logistics_no="ALS-SYNC-WARNING",
        source="qt_checked_action",
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_notification_sync=failed_sync,
    )

    result = runner(
        TaskCommand(
            "execute",
            TaskArea.SHIPMENT,
            Capability.OUTBOUND_ORDER,
            order_no=PLATFORM_ORDER_NO,
            execution_id="mark-task",
            payload={
                "system_order_no": SYSTEM_ORDER_NO,
                "logistics_no": "ALS-SYNC-WARNING",
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
        )
    )

    assert result.succeeded is True
    assert "定时扫描补偿" in result.message
    assert result.payload["notification_sync_warning"] == "temporary WMS failure"


def test_read_only_scan_honors_shutdown_cancellation(tmp_path):
    async def run_test():
        started = asyncio.Event()
        cancellation = {"requested": False}

        async def scan(
            _settings,
            _configuration,
            _task_id,
            _operator_name,
            _operator_email,
        ):
            started.set()
            while True:
                await asyncio.sleep(1)

        runner = DesktopTaskRunner(
            tmp_path,
            settings_provider=lambda: _settings(tmp_path),
            configuration_provider=lambda: {},
            custom_scan=scan,
            cancellation_provider=lambda _task_id: cancellation["requested"],
        )
        task = asyncio.create_task(
            runner.run(
                TaskCommand(
                    "scan",
                    TaskArea.CUSTOMIZATION,
                    Capability.LIST_ORDERS,
                    execution_id="shutdown-scan",
                )
            )
        )
        await started.wait()
        cancellation["requested"] = True
        return await asyncio.wait_for(task, timeout=2)

    result = asyncio.run(run_test())

    assert result.cancelled is True
    assert result.payload["shutdown_cancelled"] is True


@pytest.mark.parametrize(
    ("service_name", "command"),
    [
        (
            "api_test",
            TaskCommand("API 测试", TaskArea.MAINTENANCE, Capability.LIST_ORDERS),
        ),
        (
            "custom_scan",
            TaskCommand("定制扫描", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS),
        ),
        (
            "shipment_scan",
            TaskCommand("标发扫描", TaskArea.SHIPMENT, Capability.LIST_ORDERS),
        ),
        (
            "shipment_notification_contact_refresh",
            TaskCommand(
                "联系方式刷新",
                TaskArea.SHIPMENT,
                Capability.GET_ORDER_DETAIL,
                payload={
                    "trigger": NOTIFICATION_CONTACT_REFRESH_TRIGGER,
                    "notification_ids": [1],
                },
            ),
        ),
        (
            "shipment_notification_sync",
            TaskCommand(
                "通知补偿",
                TaskArea.SHIPMENT,
                Capability.LIST_ORDERS,
                payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
            ),
        ),
    ],
)
def test_every_read_only_task_family_honors_shutdown_cancellation(
    tmp_path,
    service_name: str,
    command: TaskCommand,
) -> None:
    async def run_test():
        started = asyncio.Event()
        cancellation = {"requested": False}

        async def blocking_service(*_args, **_kwargs):
            started.set()
            await asyncio.Future()

        runner = DesktopTaskRunner(
            tmp_path,
            settings_provider=lambda: _settings(tmp_path),
            configuration_provider=lambda: {},
            cancellation_provider=lambda _task_id: cancellation["requested"],
            **{service_name: blocking_service},
        )
        execution_command = replace(command, execution_id=f"cancel-{service_name}")
        task = asyncio.create_task(runner.run(execution_command))
        await started.wait()
        cancellation["requested"] = True
        return await asyncio.wait_for(task, timeout=2)

    result = asyncio.run(run_test())

    assert result.cancelled is True
    assert result.payload["shutdown_cancelled"] is True


@pytest.mark.parametrize(
    ("method_name", "capability"),
    [
        ("_query_logistics", Capability.ALIBABA_LOGISTICS),
        ("_prepare_alibaba_order", Capability.ALIBABA_ORDER_PREPARE),
    ],
)
def test_alibaba_read_and_prepare_tasks_honor_shutdown_cancellation(
    tmp_path,
    monkeypatch,
    method_name: str,
    capability: Capability,
) -> None:
    async def run_test():
        started = asyncio.Event()
        cancellation = {"requested": False}

        async def blocking_operation(*_args, **_kwargs):
            started.set()
            await asyncio.Future()

        runner = DesktopTaskRunner(
            tmp_path,
            settings_provider=lambda: _settings(tmp_path),
            configuration_provider=lambda: {},
            cancellation_provider=lambda _task_id: cancellation["requested"],
        )
        monkeypatch.setattr(runner, method_name, blocking_operation)
        task = asyncio.create_task(
            runner.run(
                TaskCommand(
                    "阿里只读任务",
                    TaskArea.SHIPMENT,
                    capability,
                    order_no=SYSTEM_ORDER_NO,
                    execution_id=f"cancel-{capability.value}",
                )
            )
        )
        await started.wait()
        cancellation["requested"] = True
        return await asyncio.wait_for(task, timeout=2)

    result = asyncio.run(run_test())

    assert result.cancelled is True
    assert result.payload["shutdown_cancelled"] is True


def test_notification_send_cancels_after_current_message_step(tmp_path):
    cancellation = {"requested": False}
    calls: list[tuple[int, bool, str]] = []

    def send_one(notification_id: int, retry: bool, actor: str) -> ControlResult:
        calls.append((notification_id, retry, actor))
        cancellation["requested"] = True
        return ControlResult(
            True,
            "sent",
            details={
                "notification_id": notification_id,
                "state": "DELIVERED",
                "provider_accepted": True,
            },
        )

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_notification_review_send=send_one,
        runtime_write_guard_provider=lambda: True,
        cancellation_provider=lambda _task_id: cancellation["requested"],
    )
    notification_ids = [101, 102, 103]
    confirmation_order_no = notification_confirmation_order_no(notification_ids)
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.SEND_SHIPMENT_NOTIFICATION,
        confirmation_order_no,
        source="qt_checked_action",
    )

    result = runner(
        TaskCommand(
            "send reviewed notifications",
            TaskArea.SHIPMENT,
            Capability.SEND_NOTIFICATION,
            order_no=confirmation_order_no,
            payload={
                "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                "notification_ids": notification_ids,
                DESKTOP_OPERATOR_NAME_PAYLOAD_KEY: "Steven",
                DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY: "steven@billyprint.com",
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
            execution_id="notification-send-task",
        )
    )

    assert result.cancelled is True
    assert result.payload["processed"] == 1
    assert result.payload["requested"] == 3
    assert calls == [(101, False, "steven@billyprint.com")]


def test_notification_send_all_failures_report_reason_and_fail_task(tmp_path):
    failure_message = (
        "发送未开始：通知内容、联系方式或物流信息在审核后发生变化，"
        "已生成新的待审核版本；未调用邮件或短信服务，请重新核对后发送。"
    )

    def send_one(notification_id: int, _retry: bool, _actor: str) -> ControlResult:
        return ControlResult(
            False,
            failure_message,
            details={
                "notification_id": notification_id,
                "state": "AWAITING_REVIEW",
                "provider_accepted": False,
                "send_failure_visible": True,
            },
        )

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_notification_review_send=send_one,
        runtime_write_guard_provider=lambda: True,
    )
    notification_ids = [401, 402]
    order_no = notification_confirmation_order_no(notification_ids)
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.SEND_SHIPMENT_NOTIFICATION,
        order_no,
        source="qt_checked_action",
    )

    result = runner(
        TaskCommand(
            "send failed reviewed notifications",
            TaskArea.SHIPMENT,
            Capability.SEND_NOTIFICATION,
            order_no=order_no,
            payload={
                "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                "notification_ids": notification_ids,
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
        )
    )

    assert result.succeeded is False
    assert result.payload["status"] == "failed"
    assert result.payload["failed"] == 2
    assert result.payload["provider_accepted"] == 0
    assert result.payload["failure_reasons"] == [
        {"reason": failure_message, "count": 2}
    ]
    assert f"{failure_message}（2 条）" in result.message


def test_notification_send_partial_failure_reports_warning_status(tmp_path):
    def send_one(notification_id: int, _retry: bool, _actor: str) -> ControlResult:
        if notification_id == 501:
            return ControlResult(
                False,
                "发送服务已接收，等待回执。",
                details={
                    "notification_id": notification_id,
                    "state": "ACCEPTED",
                    "provider_accepted": True,
                },
            )
        return ControlResult(
            False,
            "发送未开始：审核快照已变化。",
            details={
                "notification_id": notification_id,
                "state": "AWAITING_REVIEW",
                "provider_accepted": False,
            },
        )

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_notification_review_send=send_one,
        runtime_write_guard_provider=lambda: True,
    )
    notification_ids = [501, 502]
    order_no = notification_confirmation_order_no(notification_ids)
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.SEND_SHIPMENT_NOTIFICATION,
        order_no,
        source="qt_checked_action",
    )

    result = runner(
        TaskCommand(
            "send mixed reviewed notifications",
            TaskArea.SHIPMENT,
            Capability.SEND_NOTIFICATION,
            order_no=order_no,
            payload={
                "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                "notification_ids": notification_ids,
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
        )
    )

    assert result.succeeded is False
    assert result.payload["status"] == "completed_with_warnings"
    assert result.payload["provider_accepted"] == 1
    assert result.payload["failed"] == 1
    assert result.payload["failure_reasons"] == [
        {"reason": "发送未开始：审核快照已变化。", "count": 1}
    ]


def test_notification_send_requires_desktop_write_confirmation(tmp_path):
    calls: list[int] = []

    def send_one(notification_id: int, _retry: bool, _actor: str) -> ControlResult:
        calls.append(notification_id)
        return ControlResult(True, "sent")

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_notification_review_send=send_one,
        runtime_write_guard_provider=lambda: True,
    )
    notification_ids = [201]
    order_no = notification_confirmation_order_no(notification_ids)

    result = runner(
        TaskCommand(
            "send reviewed notification",
            TaskArea.SHIPMENT,
            Capability.SEND_NOTIFICATION,
            order_no=order_no,
            payload={
                "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                "notification_ids": notification_ids,
            },
        )
    )

    assert result.succeeded is False
    assert result.blocked is True
    assert "写入确认" in result.message
    assert calls == []


def test_notification_send_confirmation_must_match_exact_batch(tmp_path):
    calls: list[int] = []

    def send_one(notification_id: int, _retry: bool, _actor: str) -> ControlResult:
        calls.append(notification_id)
        return ControlResult(True, "sent")

    confirmed_order_no = notification_confirmation_order_no([301])
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.SEND_SHIPMENT_NOTIFICATION,
        confirmed_order_no,
        source="qt_message_box",
    )
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        shipment_notification_review_send=send_one,
        runtime_write_guard_provider=lambda: True,
    )

    result = runner(
        TaskCommand(
            "send different reviewed notification",
            TaskArea.SHIPMENT,
            Capability.SEND_NOTIFICATION,
            order_no=confirmed_order_no,
            payload={
                "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                "notification_ids": [302],
                DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
            },
        )
    )

    assert result.succeeded is False
    assert result.blocked is True
    assert "当前批次不匹配" in result.message
    assert calls == []
