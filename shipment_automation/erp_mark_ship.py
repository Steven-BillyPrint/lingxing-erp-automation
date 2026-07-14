from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable

from lingxing_automation.browser.session import get_first_page, launch_context, wait_for_order_page
from lingxing_automation.config import configuration_source_from_args, load_login_config
from lingxing_automation.constants import ORDER_MANAGEMENT_URL
from lingxing_automation.models import LoginConfig
from lingxing_automation.pages.order_detail_navigation import close_order_detail_dialog
from lingxing_automation.pages.order_list import ensure_order_view_mode
from lingxing_automation.pages.order_table_actions import (
    click_dialog_button,
    click_toolbar_button,
    click_visible_menu_item,
    dismiss_outbound_success_dialog,
    dismiss_result_dialog,
    ensure_dialog_warehouse,
    fill_dialog_form,
    open_row_operation_menu,
    search_platform_order,
    select_cascader_path,
    select_order_row,
    switch_order_tab,
    wait_for_dialog,
    wait_for_order_row,
)

from .alibaba_logistics import (
    normalize_carrier_name,
    tracking_number_matches_carrier,
    tracking_number_mismatch_reason,
)
from .config import DEFAULT_SHIPMENT_QUEUE_PATH
from .models import (
    ERP_BLOCKED,
    ERP_CHECKPOINT_AUDITED,
    ERP_CHECKPOINT_CHANNEL_SET,
    ERP_CHECKPOINT_LOGISTICS_SAVED,
    ERP_CHECKPOINT_NONE,
    ERP_CHECKPOINT_OUTBOUNDED,
    ERP_DONE,
    ERP_PENDING,
    ERP_RETRYABLE,
    ERP_WAITING,
    SALES_CHANNEL_INDEPENDENT_SITE,
    ErpMarkReport,
    ErpMarkResult,
    ReadyToMarkItem,
    StoreFulfillmentReminder,
)
from .queue_store import ShipmentQueueStore


ERP_CHANNEL_PATHS: dict[str, list[str]] = {
    "UPS": ["手动-Alibaba logistics", "UPS-阿里巴巴"],
    "FEDEX": ["手动-Alibaba logistics", "Fedex-阿里巴巴"],
    "DHL": ["手动-Alibaba logistics", "DHL-阿里巴巴"],
    "USPS": ["手动", "USPS"],
    "YANWEN": ["手动", "燕文"],
    "UNIUNI": ["手动", "UniUni"],
    "GOFO": ["手动", "GOFO"],
    "SPEEDX": ["手动", "SpeedX（不得标发亚马逊）"],
    "SWIFTX": ["手动", "SwiftX（不得标发亚马逊）"],
    "1ST": ["手动", "一代国际物流（不得标发亚马逊）"],
}

CHECKPOINT_RANK = {
    ERP_CHECKPOINT_NONE: 0,
    ERP_CHECKPOINT_CHANNEL_SET: 1,
    ERP_CHECKPOINT_AUDITED: 2,
    ERP_CHECKPOINT_LOGISTICS_SAVED: 3,
    ERP_CHECKPOINT_OUTBOUNDED: 4,
}


class ErpMarkManualReview(RuntimeError):
    pass


class ErpMarkUserAbort(RuntimeError):
    pass


class ErpMarkTrackingBlocked(RuntimeError):
    pass


class ErpMarkLeaseLost(RuntimeError):
    pass


ConfirmFunc = Callable[[str], Awaitable[bool]]
CheckpointFunc = Callable[[str, dict[str, str | None]], Awaitable[None]]
ApprovalFunc = Callable[[str, str], Awaitable[None]]
MarkItemFunc = Callable[[Any, ReadyToMarkItem, ConfirmFunc], Awaitable[str]]


def erp_channel_path_for_carrier(carrier: str | None) -> list[str]:
    key = normalize_carrier_name(carrier)
    path = ERP_CHANNEL_PATHS.get(key)
    if not path:
        raise ErpMarkManualReview(f"ERP 未配置该国际物流服务商的渠道映射：{carrier or '-'}")
    return list(path)


def clean_money_amount(value: str | None) -> str:
    text = str(value or "").replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise ErpMarkManualReview(f"物流运费不是可填写的金额：{value or '-'}")
    return match.group(0)


def format_chargeable_weight_g(value: str | None) -> str:
    text = str(value or "").replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise ErpMarkManualReview(f"计费重 KG 不是可填写的数值：{value or '-'}")
    try:
        grams = Decimal(match.group(0)) * Decimal("1000")
    except InvalidOperation as exc:
        raise ErpMarkManualReview(f"计费重 KG 不是可填写的数值：{value or '-'}") from exc
    if grams == grams.to_integral_value():
        return str(int(grams))
    return format(grams.normalize(), "f")


def erp_payload_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def channel_payload(item: ReadyToMarkItem) -> dict[str, Any]:
    return {
        "logistics_no": item.logistics_no,
        "carrier": normalize_carrier_name(item.carrier),
        "channel_path": erp_channel_path_for_carrier(item.carrier),
    }


def logistics_form_payload(item: ReadyToMarkItem) -> dict[str, str]:
    return {
        "运单号": str(item.international_tracking_no or ""),
        "跟踪号": item.logistics_no,
        "物流运费": clean_money_amount(item.actual_total),
        "计费重": format_chargeable_weight_g(item.chargeable_weight_kg),
    }


def validate_ready_item(item: ReadyToMarkItem) -> None:
    missing = []
    for field_name in (
        "system_order_no",
        "platform_order_no",
        "logistics_no",
        "carrier",
        "international_tracking_no",
        "actual_total",
        "chargeable_weight_kg",
    ):
        if not str(getattr(item, field_name) or "").strip():
            missing.append(field_name)
    if missing:
        raise ErpMarkManualReview(f"队列记录缺少 ERP 标发必填字段：{', '.join(missing)}")
    erp_channel_path_for_carrier(item.carrier)
    if (
        not item.tracking_manually_verified
        and not tracking_number_matches_carrier(item.carrier, item.international_tracking_no)
    ):
        raise ErpMarkTrackingBlocked(
            tracking_number_mismatch_reason(item.carrier, item.international_tracking_no)
        )
    clean_money_amount(item.actual_total)
    format_chargeable_weight_g(item.chargeable_weight_kg)


async def prompt_user_confirmation(prompt: str) -> bool:
    answer = await asyncio.to_thread(input, prompt)
    return answer.strip().lower() == "y"


def _tracking_blocked_warnings(items: list[dict[str, Any]]) -> list[str]:
    return [
        f"已阻止错误尾程单号：{item['platform_order_no']} / {item['logistics_no']} / {item['last_error']}"
        for item in items
    ]


def _empty_erp_mark_report(
    store: ShipmentQueueStore,
    *,
    queue_path: str,
    dry_run: bool,
    logistics_no: str | None,
    tracking_blocked: list[dict[str, Any]],
) -> dict[str, Any]:
    if logistics_no is None:
        message = "没有可执行的 ERP 标发记录。"
    elif store.get_by_logistics_no(logistics_no) is None:
        message = f"物流单号 {logistics_no} 不存在，未领取任何其他 ERP 标发任务。"
    else:
        message = (
            f"物流单号 {logistics_no} 当前不可执行"
            "（状态不满足、尚未到重试时间或已被其他任务领取），"
            "未领取任何其他 ERP 标发任务。"
        )
    report = ErpMarkReport(
        status="completed",
        message=message,
        queue_path=queue_path,
        dry_run=dry_run,
        execute=not dry_run,
        tracking_blocked_count=len(tracking_blocked),
        warnings=_tracking_blocked_warnings(tracking_blocked),
    )
    return erp_mark_report_to_dict(report)


def _attach_tracking_blocked(report: ErpMarkReport, items: list[dict[str, Any]]) -> None:
    report.tracking_blocked_count += len(items)
    report.warnings.extend(_tracking_blocked_warnings(items))


async def run_erp_mark_worker(args: argparse.Namespace) -> dict[str, Any]:
    queue_path = str(Path(getattr(args, "queue_path", DEFAULT_SHIPMENT_QUEUE_PATH)).resolve())
    limit = int(getattr(args, "limit", 0) or 0)
    dry_run = bool(getattr(args, "dry_run", True))
    logistics_no = str(getattr(args, "logistics_no", "") or "").strip() or None
    mark_item_func = getattr(args, "mark_item_func", None)
    if mark_item_func is not None and not callable(mark_item_func):
        raise TypeError("args.mark_item_func 必须是可调用的异步标发函数。")
    confirm_func = getattr(args, "confirm_func", None) or prompt_user_confirmation
    if not callable(confirm_func):
        raise TypeError("args.confirm_func 必须是可调用的异步确认函数。")
    store = ShipmentQueueStore(queue_path)
    worker_id = f"erp-{uuid.uuid4().hex}"
    run_id = uuid.uuid4().hex
    tracking_blocked = (
        []
        if dry_run or logistics_no is not None
        else store.block_invalid_tracking_records(run_id=run_id)
    )
    items = store.list_erp_mark_candidates(limit=limit, logistics_no=logistics_no)

    if not items:
        return _empty_erp_mark_report(
            store,
            queue_path=queue_path,
            dry_run=dry_run,
            logistics_no=logistics_no,
            tracking_blocked=tracking_blocked,
        )

    if dry_run:
        report = await process_erp_mark_items_once(
            store,
            items,
            page=None,
            queue_path=queue_path,
            dry_run=True,
            confirm_func=confirm_func,
            mark_item_func=mark_item_func,
        )
        return erp_mark_report_to_dict(report)

    if mark_item_func is not None:
        items = store.claimed_erp_items(
            worker_id,
            limit=limit,
            logistics_no=logistics_no,
        )
        if not items:
            return _empty_erp_mark_report(
                store,
                queue_path=queue_path,
                dry_run=False,
                logistics_no=logistics_no,
                tracking_blocked=tracking_blocked,
            )
        report = await process_erp_mark_items_once(
            store,
            items,
            page=None,
            queue_path=queue_path,
            dry_run=False,
            confirm_func=confirm_func,
            mark_item_func=mark_item_func,
            worker_id=worker_id,
            run_id=run_id,
        )
        _attach_tracking_blocked(report, tracking_blocked)
        return erp_mark_report_to_dict(report)

    login_config = LoginConfig()
    if not getattr(args, "no_auto_login", False):
        login_config = load_login_config(configuration_source_from_args(args))

    playwright, context = await launch_context(args)
    page = await get_first_page(context)
    try:
        await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
        await wait_for_order_page(
            page,
            int(getattr(args, "login_timeout_sec", 300)),
            login_config,
            auto_login=not getattr(args, "no_auto_login", False),
            debug_dir=getattr(args, "debug_log_dir", "debug/logs"),
        )
        await close_order_detail_dialog(page)
        await ensure_order_view_mode(page, debug_dir=getattr(args, "debug_log_dir", "debug/logs"))
        newly_blocked = (
            []
            if logistics_no is not None
            else store.block_invalid_tracking_records(run_id=run_id)
        )
        tracking_blocked.extend(newly_blocked)
        items = store.claimed_erp_items(
            worker_id,
            limit=limit,
            logistics_no=logistics_no,
        )
        if not items:
            return _empty_erp_mark_report(
                store,
                queue_path=queue_path,
                dry_run=False,
                logistics_no=logistics_no,
                tracking_blocked=tracking_blocked,
            )
        report = await process_erp_mark_items_once(
            store,
            items,
            page=page,
            queue_path=queue_path,
            dry_run=False,
            confirm_func=confirm_func,
            worker_id=worker_id,
            run_id=run_id,
        )
        _attach_tracking_blocked(report, tracking_blocked)
        return erp_mark_report_to_dict(report)
    finally:
        if getattr(args, "keep_browser_open", False):
            print("Browser will stay open for inspection.")
        else:
            await context.close()
        await playwright.stop()


async def process_erp_mark_items_once(
    store: ShipmentQueueStore,
    items: list[ReadyToMarkItem],
    *,
    page,
    queue_path: str,
    dry_run: bool,
    confirm_func: ConfirmFunc,
    mark_item_func: MarkItemFunc | None = None,
    worker_id: str | None = None,
    run_id: str | None = None,
) -> ErpMarkReport:
    report = ErpMarkReport(
        status="completed",
        message="ERP 标发流程完成。" if not dry_run else "ERP 标发 dry-run 完成。",
        queue_path=queue_path,
        dry_run=dry_run,
        execute=not dry_run,
        total_count=len(items),
    )

    for item in items:
        current_version = item.version
        current_checkpoint = item.erp_checkpoint
        owner = worker_id or item.lease_owner
        try:
            validate_ready_item(item)
            if dry_run:
                report.results.append(
                    ErpMarkResult(
                        system_order_no=item.system_order_no,
                        platform_order_no=item.platform_order_no,
                        logistics_no=item.logistics_no,
                        erp_step="DRY_RUN",
                        last_error="dry-run：未点击 ERP。",
                        erp_state=item.erp_state,
                        erp_checkpoint=item.erp_checkpoint,
                        carrier=item.carrier,
                        international_tracking_no=item.international_tracking_no,
                        sales_channel=item.sales_channel,
                        customer_email_required=item.customer_email_required,
                    )
                )
                continue
            if not owner or (page is None and mark_item_func is None):
                raise RuntimeError("execute 模式缺少 ERP 页面或任务租约。")

            async def checkpoint_func(checkpoint: str, values: dict[str, str | None]) -> None:
                nonlocal current_checkpoint, current_version
                new_version = store.record_erp_checkpoint(
                    item.logistics_no,
                    owner=owner,
                    expected_version=current_version,
                    checkpoint=checkpoint,
                    channel_path=values.get("channel_path"),
                    freight_amount=values.get("freight_amount"),
                    chargeable_weight_g=values.get("chargeable_weight_g"),
                    channel_payload_hash=values.get("channel_payload_hash"),
                    logistics_payload_hash=values.get("logistics_payload_hash"),
                    run_id=run_id,
                )
                if new_version is None:
                    raise ErpMarkLeaseLost(f"ERP 任务租约或版本已变化：{item.logistics_no}")
                current_version = new_version
                current_checkpoint = checkpoint

            async def approval_func(confirmation_type: str, payload_hash: str) -> None:
                if not store.record_erp_confirmation(
                    item.logistics_no,
                    owner=owner,
                    payload_hash=payload_hash,
                    confirmation_type=confirmation_type,
                    run_id=run_id,
                ):
                    raise ErpMarkLeaseLost(f"无法记录用户确认：{item.logistics_no}")

            async def leased_confirm(prompt: str) -> bool:
                store.renew_lease(item.logistics_no, owner)
                confirmed = await confirm_func(prompt)
                store.renew_lease(item.logistics_no, owner)
                if confirmed:
                    recorded = store.record_erp_prompt_confirmation(
                        item.logistics_no,
                        owner=owner,
                        prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                        confirmation_source=str(
                            getattr(confirm_func, "confirmation_source", "stdin")
                        ),
                        confirmation_id=(
                            str(getattr(confirm_func, "confirmation_id", "") or "").strip()
                            or None
                        ),
                        run_id=run_id,
                    )
                    if not recorded:
                        raise ErpMarkLeaseLost(
                            f"无法记录危险写入确认：{item.logistics_no}"
                        )
                return confirmed

            if mark_item_func is None:
                final_step = await execute_erp_mark_item(
                    page,
                    item,
                    leased_confirm,
                    checkpoint_func=checkpoint_func,
                    approval_func=approval_func,
                )
            else:
                final_step = await mark_item_func(page, item, leased_confirm)
                await checkpoint_func(ERP_CHECKPOINT_OUTBOUNDED, {})

            report.done_count += 1
            report.results.append(
                ErpMarkResult(
                    system_order_no=item.system_order_no,
                    platform_order_no=item.platform_order_no,
                    logistics_no=item.logistics_no,
                    erp_step=final_step,
                    erp_state=ERP_DONE,
                    erp_checkpoint=ERP_CHECKPOINT_OUTBOUNDED,
                    carrier=item.carrier,
                    international_tracking_no=item.international_tracking_no,
                    sales_channel=item.sales_channel,
                    customer_email_required=item.customer_email_required,
                )
            )
            if item.sales_channel == SALES_CHANNEL_INDEPENDENT_SITE:
                report.store_fulfillment_reminders.append(
                    StoreFulfillmentReminder(
                        independent_order_no=item.platform_order_no,
                        system_order_no=item.system_order_no,
                        logistics_no=item.logistics_no,
                        carrier=item.carrier,
                        international_tracking_no=item.international_tracking_no,
                    )
                )
        except ErpMarkTrackingBlocked as exc:
            report.tracking_blocked_count += 1
            if not dry_run and owner:
                changed = store.return_tracking_to_blocked(
                    item.logistics_no,
                    reason=str(exc),
                    owner=owner,
                    expected_version=current_version,
                    run_id=run_id,
                )
                if not changed:
                    raise ErpMarkLeaseLost(f"无法阻止单号不匹配记录：{item.logistics_no}")
            report.results.append(
                ErpMarkResult(
                    system_order_no=item.system_order_no,
                    platform_order_no=item.platform_order_no,
                    logistics_no=item.logistics_no,
                    erp_step="TRACKING_BLOCKED",
                    last_error=str(exc),
                    erp_state=ERP_WAITING,
                    erp_checkpoint=current_checkpoint,
                    carrier=item.carrier,
                    international_tracking_no=item.international_tracking_no,
                    sales_channel=item.sales_channel,
                    customer_email_required=item.customer_email_required,
                )
            )
        except ErpMarkUserAbort as exc:
            report.skipped_count += 1
            if not dry_run and owner:
                store.finish_erp_attempt(
                    item.logistics_no,
                    owner=owner,
                    state=ERP_PENDING,
                    last_error=str(exc),
                    expected_version=current_version,
                    run_id=run_id,
                )
            report.results.append(
                ErpMarkResult(
                    system_order_no=item.system_order_no,
                    platform_order_no=item.platform_order_no,
                    logistics_no=item.logistics_no,
                    erp_step="USER_SKIPPED",
                    last_error=str(exc),
                    erp_state=ERP_PENDING,
                    erp_checkpoint=current_checkpoint,
                    carrier=item.carrier,
                    international_tracking_no=item.international_tracking_no,
                    sales_channel=item.sales_channel,
                    customer_email_required=item.customer_email_required,
                )
            )
            if page is not None:
                try:
                    await _reset_erp_page_after_user_skip(page)
                except Exception as cleanup_exc:
                    report.warnings.append(
                        f"跳过订单后恢复 ERP 页面失败：{item.platform_order_no} / {cleanup_exc}"
                    )
        except ErpMarkManualReview as exc:
            report.blocked_count += 1
            if not dry_run and owner:
                store.finish_erp_attempt(
                    item.logistics_no,
                    owner=owner,
                    state=ERP_BLOCKED,
                    last_error=str(exc),
                    expected_version=current_version,
                    run_id=run_id,
                )
            report.results.append(
                ErpMarkResult(
                    system_order_no=item.system_order_no,
                    platform_order_no=item.platform_order_no,
                    logistics_no=item.logistics_no,
                    erp_step="BLOCKED",
                    last_error=str(exc),
                    erp_state=ERP_BLOCKED,
                    erp_checkpoint=current_checkpoint,
                    carrier=item.carrier,
                    international_tracking_no=item.international_tracking_no,
                    sales_channel=item.sales_channel,
                    customer_email_required=item.customer_email_required,
                )
            )
        except Exception as exc:
            report.retryable_count += 1
            if not dry_run and owner:
                store.finish_erp_attempt(
                    item.logistics_no,
                    owner=owner,
                    state=ERP_RETRYABLE,
                    last_error=str(exc),
                    expected_version=current_version,
                    run_id=run_id,
                )
            report.results.append(
                ErpMarkResult(
                    system_order_no=item.system_order_no,
                    platform_order_no=item.platform_order_no,
                    logistics_no=item.logistics_no,
                    erp_step="RETRYABLE",
                    last_error=str(exc),
                    erp_state=ERP_RETRYABLE,
                    erp_checkpoint=current_checkpoint,
                    carrier=item.carrier,
                    international_tracking_no=item.international_tracking_no,
                    sales_channel=item.sales_channel,
                    customer_email_required=item.customer_email_required,
                )
            )

    if report.retryable_count or report.blocked_count:
        report.status = "completed_with_errors"
        report.message = (
            f"ERP 标发批次已处理完：候选 {report.total_count}，完成 {report.done_count}，"
            f"跳过 {report.skipped_count}，BLOCKED {report.blocked_count}，"
            f"RETRYABLE {report.retryable_count}。"
        )
    elif report.skipped_count:
        report.status = "completed_with_skips"
        report.message = (
            f"ERP 标发批次已处理完：候选 {report.total_count}，完成 {report.done_count}，"
            f"跳过 {report.skipped_count}；跳过订单将在下一周期重试。"
        )
    return report


async def _reset_erp_page_after_user_skip(page) -> None:
    """Close an unconfirmed form without clicking any submit action."""
    closed = await page.evaluate(
        """
        () => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const roots = Array.from(document.querySelectorAll(
                '.el-dialog__wrapper,.el-dialog,.el-message-box,.ant-modal,.next-dialog,.modal,[role="dialog"]'
            )).filter(visible).reverse();
            for (const root of roots) {
                const controls = Array.from(root.querySelectorAll(
                    'button,a,.el-dialog__headerbtn,.el-message-box__close,.ant-modal-close,[aria-label="Close"]'
                )).filter(visible);
                const cancel = controls.find((el) => textOf(el) === '取消');
                const close = controls.find((el) =>
                    el.matches('.el-dialog__headerbtn,.el-message-box__close,.ant-modal-close,[aria-label="Close"]')
                );
                const target = cancel || close;
                if (target) {
                    target.click();
                    return true;
                }
            }
            return false;
        }
        """
    )
    if closed:
        await page.wait_for_timeout(500)
    await close_order_detail_dialog(page)
    await switch_order_tab(page, "待审核")


async def execute_erp_mark_item(
    page,
    item: ReadyToMarkItem,
    confirm_func: ConfirmFunc,
    *,
    checkpoint_func: CheckpointFunc | None = None,
    approval_func: ApprovalFunc | None = None,
) -> str:
    checkpoint_func = checkpoint_func or _noop_checkpoint
    approval_func = approval_func or _noop_approval
    checkpoint = item.erp_checkpoint or ERP_CHECKPOINT_NONE
    rank = CHECKPOINT_RANK.get(checkpoint)
    if rank is None:
        raise ErpMarkManualReview(f"队列包含未知 ERP 检查点：{checkpoint}")

    channel_path = erp_channel_path_for_carrier(item.carrier)
    form_values = logistics_form_payload(item)
    channel_hash = erp_payload_hash(channel_payload(item))
    logistics_hash = erp_payload_hash(form_values)

    if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_CHANNEL_SET]:
        rowid = await _select_expected_row(page, item, tab_text="待审核", timeout_sec=20)
        await open_row_operation_menu(page, rowid)
        await click_visible_menu_item(page, "设置仓库物流")
        await wait_for_dialog(page, "设定仓库物流")
        await ensure_dialog_warehouse(page, "设定仓库物流")
        await select_cascader_path(page, "设定仓库物流", "物流渠道", channel_path)
        await click_dialog_button(page, "设定仓库物流", "确定")
        await checkpoint_func(
            ERP_CHECKPOINT_CHANNEL_SET,
            {"channel_path": " > ".join(channel_path), "channel_payload_hash": channel_hash},
        )
        rank = CHECKPOINT_RANK[ERP_CHECKPOINT_CHANNEL_SET]

    if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_AUDITED]:
        if item.channel_payload_hash != channel_hash:
            if not await confirm_func(_confirmation_prompt(item, "仓库物流已设置，请在 ERP 中审核；输入 y 后继续审核并物流下单：")):
                raise ErpMarkUserAbort(f"用户未确认继续审核：{item.platform_order_no} / {item.logistics_no}")
            await approval_func("channel", channel_hash)
        await _select_expected_row(page, item, tab_text="待审核", timeout_sec=20)
        await click_toolbar_button(page, "审核")
        await wait_for_dialog(page, "确认审核发货")
        await click_dialog_button(page, "确认审核发货", "审核")
        await dismiss_result_dialog(page)
        await page.wait_for_timeout(3000)
        await checkpoint_func(ERP_CHECKPOINT_AUDITED, {})
        rank = CHECKPOINT_RANK[ERP_CHECKPOINT_AUDITED]

    if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_LOGISTICS_SAVED]:
        rowid = await _select_expected_row(page, item, tab_text="物流下单", timeout_sec=60)
        await open_row_operation_menu(page, rowid)
        await click_visible_menu_item(page, "编辑物流单号")
        await wait_for_dialog(page, "编辑运单号")
        await fill_dialog_form(page, "编辑运单号", form_values)
        if item.logistics_payload_hash != logistics_hash:
            if not await confirm_func(_confirmation_prompt(item, "物流信息已填写，请在 ERP 弹窗中审核；输入 y 后将确认并继续出库：")):
                raise ErpMarkUserAbort(f"用户未确认物流信息表单：{item.platform_order_no} / {item.logistics_no}")
            await approval_func("logistics", logistics_hash)
        await click_dialog_button(page, "编辑运单号", "确认")
        await page.wait_for_timeout(2500)
        await checkpoint_func(
            ERP_CHECKPOINT_LOGISTICS_SAVED,
            {
                "freight_amount": form_values["物流运费"],
                "chargeable_weight_g": form_values["计费重"],
                "logistics_payload_hash": logistics_hash,
            },
        )
        rank = CHECKPOINT_RANK[ERP_CHECKPOINT_LOGISTICS_SAVED]

    if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_OUTBOUNDED]:
        await _select_expected_row(page, item, tab_text="待打单", timeout_sec=60)
        await click_toolbar_button(page, "出库")
        await wait_for_dialog(page, "发货")
        await click_dialog_button(page, "发货", "确定")
        await dismiss_outbound_success_dialog(page)
        await page.wait_for_timeout(2500)
        await checkpoint_func(ERP_CHECKPOINT_OUTBOUNDED, {})

    await switch_order_tab(page, "待审核")
    return ERP_CHECKPOINT_OUTBOUNDED


async def _select_expected_row(page, item: ReadyToMarkItem, *, tab_text: str, timeout_sec: int) -> str:
    await switch_order_tab(page, tab_text)
    await search_platform_order(page, item.platform_order_no)
    try:
        row = await wait_for_order_row(
            page,
            system_order_no=item.system_order_no,
            platform_order_no=item.platform_order_no,
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        raise ErpMarkManualReview(
            f"ERP 页面状态与检查点不一致：在“{tab_text}”找不到系统单号 "
            f"{item.system_order_no} / 平台单号 {item.platform_order_no}。"
        ) from exc
    rowid = str(row.get("rowid") or item.system_order_no)
    await select_order_row(page, rowid)
    return rowid


async def _noop_checkpoint(_checkpoint: str, _values: dict[str, str | None]) -> None:
    return None


async def _noop_approval(_confirmation_type: str, _payload_hash: str) -> None:
    return None


def erp_mark_report_to_dict(report: ErpMarkReport) -> dict[str, Any]:
    return asdict(report)


def _confirmation_prompt(item: ReadyToMarkItem, title: str) -> str:
    return (
        f"\n{title}\n"
        f"系统单号：{item.system_order_no}\n"
        f"平台单号：{item.platform_order_no}\n"
        f"物流单号：{item.logistics_no}\n"
        f"国际物流服务商：{item.carrier or '-'}\n"
        f"国际物流单号：{item.international_tracking_no or '-'}\n"
        "请输入 y 继续，其他输入跳过当前订单并检查下一单："
    )
