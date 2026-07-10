from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable

from lingxing_automation.browser.session import get_first_page, launch_context, wait_for_order_page
from lingxing_automation.config import load_login_config
from lingxing_automation.constants import ORDER_MANAGEMENT_URL
from lingxing_automation.models import LoginConfig
from lingxing_automation.pages.order_detail_navigation import close_order_detail_dialog
from lingxing_automation.pages.order_list import ensure_order_view_mode
from lingxing_automation.pages.order_table_actions import (
    click_dialog_button,
    click_toolbar_button,
    click_visible_menu_item,
    dismiss_result_dialog,
    fill_dialog_form,
    open_row_operation_menu,
    search_platform_order,
    select_cascader_path,
    select_order_row,
    switch_order_tab,
    wait_for_dialog,
    wait_for_order_row,
)

from .alibaba_logistics import normalize_carrier_name
from .config import DEFAULT_SHIPMENT_QUEUE_PATH
from .models import (
    ErpMarkReport,
    ErpMarkResult,
    QUEUE_STATUS_ERP_MARKED,
    QUEUE_STATUS_ERROR,
    QUEUE_STATUS_MANUAL_REVIEW,
    QUEUE_STATUS_READY_TO_MARK,
    ReadyToMarkItem,
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


class ErpMarkManualReview(RuntimeError):
    pass


class ErpMarkUserAbort(RuntimeError):
    pass


ConfirmFunc = Callable[[str], Awaitable[bool]]
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


def validate_ready_item(item: ReadyToMarkItem) -> None:
    missing = []
    for field_name in (
        "system_order_no",
        "platform_order_no",
        "als_no",
        "logistics_order_no",
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
    clean_money_amount(item.actual_total)
    format_chargeable_weight_g(item.chargeable_weight_kg)


async def prompt_user_confirmation(prompt: str) -> bool:
    answer = await asyncio.to_thread(input, prompt)
    return answer.strip().lower() == "y"


async def run_erp_mark_worker(args: argparse.Namespace) -> dict[str, Any]:
    """Run phase-three ERP marking for READY_TO_MARK queue records."""

    queue_path = str(Path(getattr(args, "queue_path", DEFAULT_SHIPMENT_QUEUE_PATH)).resolve())
    limit = int(getattr(args, "limit", 0) or 0)
    dry_run = bool(getattr(args, "dry_run", True))
    store = ShipmentQueueStore(queue_path)
    items = store.list_erp_mark_candidates(limit=limit)

    if not items:
        report = ErpMarkReport(
            status="completed",
            message="没有 READY_TO_MARK 或可重试 ERROR 记录需要 ERP 标发。",
            queue_path=queue_path,
            dry_run=dry_run,
            execute=not dry_run,
        )
        return erp_mark_report_to_dict(report)

    if dry_run:
        report = await process_erp_mark_items_once(
            store,
            items,
            page=None,
            queue_path=queue_path,
            dry_run=True,
            confirm_func=prompt_user_confirmation,
        )
        return erp_mark_report_to_dict(report)

    login_config = LoginConfig()
    if not getattr(args, "no_auto_login", False):
        login_config = load_login_config(getattr(args, "env_path", ".env"))

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
        report = await process_erp_mark_items_once(
            store,
            items,
            page=page,
            queue_path=queue_path,
            dry_run=False,
            confirm_func=prompt_user_confirmation,
        )
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
) -> ErpMarkReport:
    report = ErpMarkReport(
        status="completed",
        message="ERP 标发流程完成。" if not dry_run else "ERP 标发 dry-run 完成。",
        queue_path=queue_path,
        dry_run=dry_run,
        execute=not dry_run,
        total_count=len(items),
    )
    mark_item = mark_item_func or execute_erp_mark_item

    for item in items:
        try:
            validate_ready_item(item)
            if dry_run:
                report.results.append(
                    ErpMarkResult(
                        system_order_no=item.system_order_no,
                        platform_order_no=item.platform_order_no,
                        als_no=item.als_no,
                        erp_step="DRY_RUN",
                        queue_status=QUEUE_STATUS_READY_TO_MARK,
                        last_error="dry-run：未点击 ERP。",
                    )
                )
                continue

            if page is None:
                raise RuntimeError("execute 模式缺少 ERP 页面。")
            final_step = await mark_item(page, item, confirm_func)
            store.update_erp_mark_by_als(item.als_no, queue_status=QUEUE_STATUS_ERP_MARKED, last_error=None)
            report.marked_count += 1
            report.results.append(
                ErpMarkResult(
                    system_order_no=item.system_order_no,
                    platform_order_no=item.platform_order_no,
                    als_no=item.als_no,
                    erp_step=final_step,
                    queue_status=QUEUE_STATUS_ERP_MARKED,
                )
            )
        except ErpMarkUserAbort as exc:
            report.status = "aborted"
            report.message = str(exc)
            report.results.append(
                ErpMarkResult(
                    system_order_no=item.system_order_no,
                    platform_order_no=item.platform_order_no,
                    als_no=item.als_no,
                    erp_step="USER_ABORT",
                    queue_status=QUEUE_STATUS_READY_TO_MARK,
                    last_error=str(exc),
                )
            )
            break
        except ErpMarkManualReview as exc:
            report.manual_review_count += 1
            if not dry_run:
                store.update_erp_mark_by_als(
                    item.als_no,
                    queue_status=QUEUE_STATUS_MANUAL_REVIEW,
                    last_error=str(exc),
                    processed=False,
                )
            report.results.append(
                ErpMarkResult(
                    system_order_no=item.system_order_no,
                    platform_order_no=item.platform_order_no,
                    als_no=item.als_no,
                    erp_step="MANUAL_REVIEW",
                    queue_status=QUEUE_STATUS_MANUAL_REVIEW,
                    last_error=str(exc),
                )
            )
        except Exception as exc:
            report.error_count += 1
            if not dry_run:
                store.update_erp_mark_by_als(
                    item.als_no,
                    queue_status=QUEUE_STATUS_ERROR,
                    last_error=str(exc),
                    processed=False,
                )
            report.results.append(
                ErpMarkResult(
                    system_order_no=item.system_order_no,
                    platform_order_no=item.platform_order_no,
                    als_no=item.als_no,
                    erp_step="ERROR",
                    queue_status=QUEUE_STATUS_ERROR,
                    last_error=str(exc),
                )
            )

    if report.status == "completed" and (report.error_count or report.manual_review_count):
        report.status = "completed_with_errors"
        report.message = "ERP 标发流程完成，但存在失败或需要人工复核的记录。"
    return report


async def execute_erp_mark_item(page, item: ReadyToMarkItem, confirm_func: ConfirmFunc) -> str:
    channel_path = erp_channel_path_for_carrier(item.carrier)
    freight_amount = clean_money_amount(item.actual_total)
    chargeable_weight_g = format_chargeable_weight_g(item.chargeable_weight_kg)

    await switch_order_tab(page, "待审核")
    await search_platform_order(page, item.platform_order_no)
    row = await wait_for_order_row(
        page,
        system_order_no=item.system_order_no,
        platform_order_no=item.platform_order_no,
        timeout_sec=20,
    )
    rowid = str(row.get("rowid") or item.system_order_no)
    await select_order_row(page, rowid)
    await open_row_operation_menu(page, rowid)
    await click_visible_menu_item(page, "设置仓库物流")
    await wait_for_dialog(page, "设定仓库物流")
    await select_cascader_path(page, "设定仓库物流", "物流渠道", channel_path)
    await click_dialog_button(page, "设定仓库物流", "确定")

    if not await confirm_func(_confirmation_prompt(item, "仓库物流已设置，请在 ERP 中审核；输入 y 后继续审核并物流下单：")):
        raise ErpMarkUserAbort(f"用户未确认继续审核：{item.platform_order_no} / {item.als_no}")

    await search_platform_order(page, item.platform_order_no)
    row = await wait_for_order_row(
        page,
        system_order_no=item.system_order_no,
        platform_order_no=item.platform_order_no,
        timeout_sec=20,
    )
    rowid = str(row.get("rowid") or item.system_order_no)
    await select_order_row(page, rowid)
    await click_toolbar_button(page, "审核")
    await wait_for_dialog(page, "确认审核发货")
    await click_dialog_button(page, "确认审核发货", "审核")
    await dismiss_result_dialog(page)
    await page.wait_for_timeout(3000)

    await switch_order_tab(page, "物流下单")
    await search_platform_order(page, item.platform_order_no)
    row = await wait_for_order_row(
        page,
        system_order_no=item.system_order_no,
        platform_order_no=item.platform_order_no,
        timeout_sec=60,
    )
    rowid = str(row.get("rowid") or item.system_order_no)
    await select_order_row(page, rowid)
    await open_row_operation_menu(page, rowid)
    await click_visible_menu_item(page, "编辑物流单号")
    await wait_for_dialog(page, "编辑运单号")
    await fill_dialog_form(
        page,
        "编辑运单号",
        {
            "运单号": str(item.international_tracking_no or ""),
            "跟踪号": str(item.logistics_order_no or ""),
            "物流运费": freight_amount,
            "计费重": chargeable_weight_g,
        },
    )

    if not await confirm_func(_confirmation_prompt(item, "物流信息已填写，请在 ERP 弹窗中审核；输入 y 后将确认并继续出库：")):
        raise ErpMarkUserAbort(f"用户未确认物流信息表单：{item.platform_order_no} / {item.als_no}")

    await click_dialog_button(page, "编辑运单号", "确认")
    await page.wait_for_timeout(2500)

    await switch_order_tab(page, "待打单")
    await search_platform_order(page, item.platform_order_no)
    row = await wait_for_order_row(
        page,
        system_order_no=item.system_order_no,
        platform_order_no=item.platform_order_no,
        timeout_sec=60,
    )
    rowid = str(row.get("rowid") or item.system_order_no)
    await select_order_row(page, rowid)
    await click_toolbar_button(page, "出库")
    await wait_for_dialog(page, "发货")
    await click_dialog_button(page, "发货", "确定")
    await dismiss_result_dialog(page)
    await page.wait_for_timeout(2500)
    await switch_order_tab(page, "待审核")
    return "ERP_MARKED"


def erp_mark_report_to_dict(report: ErpMarkReport) -> dict[str, Any]:
    return asdict(report)


def _confirmation_prompt(item: ReadyToMarkItem, title: str) -> str:
    return (
        f"\n{title}\n"
        f"系统单号：{item.system_order_no}\n"
        f"平台单号：{item.platform_order_no}\n"
        f"物流单号：{item.als_no}\n"
        f"国际物流服务商：{item.carrier or '-'}\n"
        f"国际物流单号：{item.international_tracking_no or '-'}\n"
        f"物流订单号：{item.logistics_order_no or '-'}\n"
        "请输入 y 继续："
    )
