from __future__ import annotations

import asyncio
import builtins
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from erp_automation.application.desktop_tasks import DesktopTaskRunner
from erp_automation.persistence import (
    CustomWorkflowStore,
    StageRetryReviewResolution,
    WorkflowPauseKind,
    WorkflowStageState,
)
from erp_automation.ui.models import (
    Capability,
    DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    DesktopSettings,
    DesktopInteractionResponse,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
    TaskArea,
    TaskCommand,
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


PLATFORM_ORDER_NO = "111-2222222-3333333"
SYSTEM_ORDER_NO = "103000000000000001"


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


def _settings(tmp_path) -> DesktopSettings:
    return DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path=str(tmp_path / "state.sqlite3"),
        queue_path=str(tmp_path / "shipment.sqlite3"),
        browser_profile=str(tmp_path / "browser"),
        log_dir=str(tmp_path / "logs"),
    )


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
    observed: list[tuple[TaskArea, str | None]] = []

    def scanner_for(area: TaskArea):
        async def scan(
            _settings: DesktopSettings,
            _configuration: dict[str, Any],
            execution_id: str | None,
        ) -> dict[str, Any]:
            observed.append((area, execution_id))
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
            )
        )
        assert result.succeeded is True

    assert observed == [
        (TaskArea.MAINTENANCE, "task-maintenance"),
        (TaskArea.CUSTOMIZATION, "task-customization"),
        (TaskArea.SHIPMENT, "task-shipment"),
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

    async def forbidden_shipment_scan(_settings, _configuration, execution_id):
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
    assert calls == [("notification", "notification-task")]


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


def test_single_incomplete_contact_skips_selection_popup_and_keeps_folder_details(
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
    assert [request["stage"] for request in requests] == [
        "contact_writeback",
        "folder_creation",
    ]
    folder_message = requests[-1]["message"]
    expected_name = f"{PLATFORM_ORDER_NO}+1个测试产品+Buyer+直接制作"
    assert f"平台单号：{PLATFORM_ORDER_NO}" in folder_message
    assert f"系统单号：{SYSTEM_ORDER_NO}" in folder_message
    assert "文件夹状态：folder_preview" in folder_message
    assert f"文件夹名：{expected_name}" in folder_message
    assert "完整文件夹名：" not in folder_message
    assert "实际文件夹名：" not in folder_message
    assert "完整路径：" in folder_message
    assert (
        "组件：\n"
        f"  1. {PLATFORM_ORDER_NO}\n"
        "  2. 1个测试产品\n"
        "  3. Buyer\n"
        "  4. 直接制作\n"
    ) in folder_message
    assert "警告：-" in folder_message


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

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
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
    assert len(result.payload["desktop_auto_approved_prompt_hashes"]) == 1
    assert len(result.payload["desktop_user_confirmed_prompt_hashes"]) == 1
    assert len(interaction_requests) == 1
    assert interaction_requests[-1]["stage"] == "erp_mark:waybill_review"
    assert interaction_requests[-1]["title"] == "审核运单填写信息"
    assert interaction_requests[-1]["approve_label"] == "确认写入运单"


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
    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: _settings(tmp_path),
        configuration_provider=lambda: {},
        interaction_handler=lambda **_request: pytest.fail(
            "常规标发阶段不应创建桌面交互"
        ),
    )

    result = runner(command)

    assert result.succeeded is True
    assert len(result.payload["desktop_auto_approved_prompt_hashes"]) == 1
    assert result.payload["desktop_user_confirmed_prompt_hashes"] == []


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

        async def scan(_settings, _configuration, _task_id):
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
