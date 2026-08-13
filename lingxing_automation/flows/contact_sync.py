from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .batch_runtime import print_batch_round_summary, wait_before_next_round
from ..browser.session import get_first_page, launch_context, wait_for_order_page
from ..config import configuration_source_from_args, load_login_config
from ..constants import DEFAULT_PAYMENT_WINDOW_HOURS, ORDER_MANAGEMENT_URL
from ..models import (
    BatchOrderItem,
    ContactInfo,
    FolderBuildResult,
    FolderNameShortenResult,
    LoginConfig,
    OrderFolderLine,
    SyncResult,
    format_rule_missing_lines,
)
from ..pages.order_detail import (
    assert_current_detail_order,
    close_order_detail_dialog,
    collect_detail_contact_candidates,
    click_system_order,
    find_contact_from_system_orders,
    read_detail_recipient_name,
    read_shipping_contact_values,
    update_current_detail_contact,
    update_contact_for_system_orders,
    verify_saved_contact_values,
    wait_for_detail,
)
from ..pages.order_management import (
    build_batch_candidates_from_rows,
    collect_batch_order_candidates,
    ensure_order_view_mode,
    fill_order_search,
    find_system_orders_for_order_no,
    find_visible_system_order_no,
    get_order_search_snapshot,
    wait_for_visible_batch_order_rows,
    wait_for_orders_in_list,
)
from ..parsers.dates import classify_recent_payment_window, latest_payment_text
from ..parsers.contact import (
    contact_choice_identity,
    extract_contact_candidates_from_json_items,
    has_supported_contact_prompt,
    missing_contact_fields,
    normalize_text,
)
from ..parsers.orders import guess_search_kind, validate_search_snapshot
from ..products.car_magnets import PRODUCT_TYPE_CAR_MAGNET
from ..products.catalog import PRODUCT_TYPE_TENT, extract_asins, match_supported_product
from ..services.folder_builder import (
    DEFAULT_FOLDER_ROOT,
    FOLDER_EXISTING_PLATFORM_ORDER,
    SUCCESS_FOLDER_STATUSES,
    build_and_create_order_folder_from_lines,
    create_order_folder_from_preview,
)
from ..services.amazon_order_quantity import (
    AMAZON_ORDER_SUMMARY_RESOLVED,
    AMAZON_QUANTITY_RESOLVED,
    AmazonOrderQuantityClient,
    AmazonOrderQuantityResult,
    AmazonOrderSummaryResult,
)
from ..services.custom_attachment_downloader import (
    CUSTOM_ZIP_SKIPPED_NO_FOLDER,
)
from ..services.custom_zip_downloader import (
    CUSTOM_ZIP_DISABLED,
    CUSTOM_ZIP_DOWNLOAD_ERROR,
    CUSTOM_ZIP_NOT_FOUND,
    download_order_custom_zip_bundle,
)
from ..services.custom_zip_parser import (
    CUSTOM_ZIP_MOVED,
    cleanup_custom_zip_staging_dir,
    copy_custom_zip_files_to_folder,
    parse_order_custom_zip_bundle,
    write_full_folder_name_txt,
)
from ..services.custom_order_api import CustomOrderApiContext, CustomOrderApiOperations
from ..services.china_workday import (
    CHINA_TIMEZONE,
    build_processing_instruction_customer_remark,
)
from ..services.high_value_custom_order import (
    HIGH_VALUE_WORKFLOW_KIND,
    NON_TENT_HIGH_VALUE_PRODUCT_TYPES,
    build_high_value_package_split_plan,
    build_high_value_sku_plan,
    evaluate_high_value_split,
)
from ..services.order_line_matcher import (
    CustomJsonAmbiguousSameAsinError,
    OrderLineMatchError,
    build_order_folder_lines_from_json,
)
from ..services.tent_package_split_adjuster import execute_tent_package_split
from ..services.tent_package_split_planner import (
    TentPackageSplitPlan,
    build_tent_package_split_plan,
)
from ..services.tent_sku_adjuster import (
    DetailShippingDestination,
    execute_tent_sku_adjustment,
    read_detail_shipping_destination,
    read_detail_shipping_address_text,
    read_list_shipping_deadline_text,
    upsert_instruction_customer_remark,
)
from ..services.tent_sku_planner import (
    TentSkuAdjustmentPlan,
    TentSkuPlanAction,
    build_tent_sku_plan,
    format_tent_sku_plan_for_cmd,
    normalize_us_postal_code,
    parse_destination_region,
)
from ..services.tent_sku_rules import INSTRUCTION_SKU
from ..services.tent_warehouse_routing import (
    TentRoutingItem,
    TentRoutingPackage,
    TentWarehouseRoutingPlan,
    tent_sku_plan_from_routing_input,
    tent_sku_plan_to_routing_input,
)
from ..storage.dedupe import (
    append_instruction_remark_platform_order,
    append_contact_writeback_platform_order,
    append_folder_complete_platform_order,
    append_package_split_platform_order,
    append_processed_platform_order,
    append_sku_adjustment_platform_order,
    append_warehouse_logistics_platform_order,
    is_contact_writeback_done,
    is_folder_complete,
    is_instruction_remark_done,
    is_package_split_done,
    is_platform_order_processed,
    is_sku_adjustment_done,
    is_warehouse_logistics_done,
    load_order_workflow_record,
    load_processed_platform_orders,
    migrate_dedupe_file,
    update_warehouse_logistics_plan_input,
)
from ..storage.dedupe_schema import normalize_bool


WritebackConfirm = Callable[[dict[str, Any]], Awaitable[bool]]
FolderConfirm = Callable[[str, str, FolderBuildResult], Awaitable[bool]]
PlanConfirm = Callable[[Any], Awaitable[bool]]
ManualSkuConfirm = Callable[[str, str, str | None], Awaitable[bool]]
ContactChoice = Callable[[str, str, list[ContactInfo]], Awaitable[ContactInfo | None]]
NotificationContactCapture = Callable[
    [str, str, str, ContactInfo], Awaitable[bool]
]
RuntimeWriteGuard = Callable[[str, str, str], Awaitable[bool]]
BrowserFallbackConfirm = Callable[[str, str, bool], Awaitable[bool]]
InstructionRemarkConfirm = Callable[[str, str, str], Awaitable[bool]]


@dataclass(frozen=True)
class CustomOrderInteractionPolicy:
    """Injectable confirmations for non-interactive desktop execution.

    CLI callers leave this unset and keep every existing ``input()`` prompt.
    The packaged desktop supplies callbacks backed by its visible confirmation
    dialog, so a ``console=False`` process never reads stdin.
    """

    confirm_writeback: WritebackConfirm
    confirm_folder_creation: FolderConfirm
    confirm_sku_plan: PlanConfirm
    confirm_manual_sku_done: ManualSkuConfirm
    confirm_package_split_plan: PlanConfirm
    confirm_manual_package_split_done: PlanConfirm
    choose_contact: ContactChoice
    runtime_write_guard: RuntimeWriteGuard
    confirm_browser_fallback: BrowserFallbackConfirm | None = None
    confirm_instruction_remark: InstructionRemarkConfirm | None = None
    capture_notification_contact: NotificationContactCapture | None = None
    confirm_warehouse_logistics_plan: PlanConfirm | None = None


@dataclass(frozen=True)
class ValidatedOrderSearchContext:
    """One already-executed exact order search that can be safely reused."""

    order_no: str
    search_kind: str
    system_order_nos: tuple[str, ...]
    search_meta: Mapping[str, Any]
    search_duration_ms: int
    browser_search_count: int = 1


@dataclass(frozen=True)
class RetryOrderCandidateSelection:
    candidates: tuple[BatchOrderItem, ...]
    search_context: ValidatedOrderSearchContext


class _LazyContactOrderPage:
    """Launch the ERP page only when contact writeback first touches it."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.playwright = None
        self.context = None
        self.page = None

    async def _ensure(self):
        if self.page is not None:
            return self.page
        login_config = LoginConfig()
        if not self.args.no_auto_login:
            login_config = load_login_config(configuration_source_from_args(self.args))
        self.playwright, self.context = await launch_context(self.args)
        self.page = await get_first_page(self.context)
        if "mpOrderManagement" not in self.page.url:
            await self.page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
        await wait_for_order_page(
            self.page,
            self.args.login_timeout_sec,
            login_config,
            auto_login=not self.args.no_auto_login,
            debug_dir=getattr(self.args, "debug_log_dir", "debug/logs"),
        )
        if "mpOrderManagement" not in self.page.url:
            await self.page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
            await wait_for_order_page(
                self.page,
                self.args.login_timeout_sec,
                login_config,
                auto_login=not self.args.no_auto_login,
                debug_dir=getattr(self.args, "debug_log_dir", "debug/logs"),
            )
        return self.page

    async def evaluate(self, *args: Any, **kwargs: Any):
        page = await self._ensure()
        return await page.evaluate(*args, **kwargs)

    async def screenshot(self, *args: Any, **kwargs: Any):
        page = await self._ensure()
        return await page.screenshot(*args, **kwargs)

    @property
    def url(self) -> str:
        return self.page.url if self.page is not None else ORDER_MANAGEMENT_URL

    def __getattr__(self, name: str):
        if self.page is None:
            raise RuntimeError(
                "联系方式网页会话尚未初始化；API 路径不得提前访问页面对象。"
            )
        return getattr(self.page, name)

    async def close(self) -> None:
        if self.context is not None and not self.args.keep_browser_open:
            await self.context.close()
        if self.playwright is not None:
            await self.playwright.stop()


def retry_no_candidate_outcome(debug: Mapping[str, Any]) -> dict[str, Any]:
    """Describe an empty safe-retry result without guessing an order candidate."""

    wait_result = debug.get("wait_for_visible_rows")
    searched_system_orders = tuple(
        str(value).strip()
        for value in debug.get("system_order_nos_after_search") or ()
        if str(value).strip()
    )
    if (
        searched_system_orders
        and isinstance(wait_result, Mapping)
        and wait_result.get("ok") is False
    ):
        return {
            "status": "lingxing_server_error",
            "error_type": "lingxing_server_error",
            "retryable": True,
            "message": (
                "领星服务器异常：已搜索到该订单，但领星订单表格数据未能加载。"
                "本次未执行任何订单修改，请稍后重试。"
            ),
        }
    return {
        "status": "retry_no_candidate",
        "message": "已按平台单号搜索，但没有从批量表格行构造出可重测候选。",
    }


@dataclass(frozen=True)
class ContactWritebackResult:
    status: str
    completed: bool
    mutated: bool
    message: str
    before_values: Mapping[str, str]


def _contact_write_delta(
    contact: ContactInfo,
    current_values: Mapping[str, str],
) -> ContactInfo:
    """Return only fields whose normalized current value differs."""

    phone = contact.phone
    if phone and verify_saved_contact_values(
        ContactInfo(phone, None, contact.source_count, contact.source_excerpt),
        dict(current_values),
    ) is None:
        phone = None
    email = contact.email
    if email and verify_saved_contact_values(
        ContactInfo(None, email, contact.source_count, contact.source_excerpt),
        dict(current_values),
    ) is None:
        email = None
    return ContactInfo(
        phone=phone,
        email=email,
        source_count=contact.source_count,
        source_excerpt=contact.source_excerpt,
        customization_text=contact.customization_text,
    )


async def _reuse_validated_order_search(
    page,
    context: ValidatedOrderSearchContext,
) -> tuple[bool, dict[str, Any], list[str]]:
    """Revalidate controls and visible row identities without clicking search."""

    started = time.monotonic()
    snapshot = await get_order_search_snapshot(page)
    input_index = snapshot.get("searchInputIndex")
    target_label = "系统单号" if context.search_kind == "system" else "平台单号"
    ok, message = validate_search_snapshot(
        context.order_no,
        target_label,
        snapshot.get("selectedLabel"),
        snapshot.get("inputs", []),
        input_index if isinstance(input_index, int) else -1,
    )
    current_system_order_nos = list(
        dict.fromkeys(
            await find_system_orders_for_order_no(
                page,
                context.order_no,
                context.search_kind,
            )
        )
    )
    expected = list(dict.fromkeys(context.system_order_nos))
    identities_match = current_system_order_nos == expected
    reused = bool(ok and expected and identities_match)
    meta = {
        **dict(context.search_meta),
        "search_context_reused": reused,
        "search_context_validation_message": (
            "已复用候选扫描的搜索结果。"
            if reused
            else message
            if not ok
            else (
                "当前表格订单身份已变化，执行一次安全重搜："
                f"期望 {expected}，实际 {current_system_order_nos}。"
            )
        ),
        "search_context_validation_ms": round(
            (time.monotonic() - started) * 1000
        ),
    }
    return reused, meta, current_system_order_nos


async def runtime_write_allowed(
    interaction_policy: CustomOrderInteractionPolicy | None,
    stage: str,
    platform_order_no: str,
    system_order_no: str,
) -> bool:
    """Re-check the desktop emergency stop immediately before a write.

    CLI workflows do not inject an interaction policy and retain their existing
    prompts.  The desktop policy is deliberately queried on every call; a task
    must not cache the switch state observed when it started.
    """

    if interaction_policy is None:
        return True
    try:
        return bool(
            await interaction_policy.runtime_write_guard(
                stage,
                platform_order_no,
                system_order_no,
            )
        )
    except Exception:
        # A broken/missing runtime policy provider is a safety failure.  Treat
        # it exactly like an active emergency stop and do not attempt the write.
        return False


def mark_runtime_write_blocked(
    payload: dict[str, Any],
    *,
    stage: str,
    stage_label: str,
    status_key: str,
    error_key: str,
) -> str:
    """Attach a machine-readable blocked result without exposing secrets."""

    message = f"运行时写入保护已暂停 {stage_label} 阶段；请检查桌面急停状态后重新处理。"
    payload[status_key] = "paused_by_emergency_stop"
    payload[error_key] = message
    payload["runtime_write_guard_blocked"] = True
    payload["runtime_write_guard_stage"] = stage
    payload["manual_review_required"] = False
    return message

async def confirm_writeback_in_cmd(context: dict[str, Any]) -> bool:
    """在命令行确认联系方式写回内容，避免误写订单。"""
    expected_system_order_no = context.get("expected_system_order_no") or "-"
    expected_platform_order_no = context.get("expected_platform_order_no") or "-"
    current_identity = context.get("current_identity") or {}
    before_values = context.get("before_values") or {}
    after_fill_values = context.get("after_fill_values") or {}
    phone = context.get("phone")
    email = context.get("email")
    current_system_order_no = current_identity.get("system_order_no") or "-"
    current_platform_order_nos = [str(item) for item in current_identity.get("platform_order_nos") or []]
    system_ok = current_system_order_no == expected_system_order_no
    platform_ok = expected_platform_order_no in current_platform_order_nos

    print("\n[保存前确认]")
    print(f"平台单号：{expected_platform_order_no}")
    print(f"系统单号：{expected_system_order_no}")
    print(f"订单校验：{'通过' if system_ok and platform_ok else '不匹配'}")
    if phone:
        print(f"电话：{before_values.get('phone') or '-'} -> {after_fill_values.get('phone') or phone}")
    else:
        print("电话：定制化信息未提取，本次不写入")
    if email:
        print(f"买家邮箱：{before_values.get('email') or '-'} -> {after_fill_values.get('email') or email}")
    else:
        print("买家邮箱：定制化信息未提取，本次不写入")
    answer = await asyncio.to_thread(input, "确认保存请输入 y；输入其它内容取消并跳过该订单：")
    return answer.strip().lower() in {"y", "yes", "1"}


async def approve_preconfirmed_writeback(_context: dict[str, Any]) -> bool:
    """Avoid a second prompt after one combined API/browser confirmation."""

    return True


async def confirm_folder_creation_in_cmd(
    platform_order_no: str,
    system_order_no: str,
    folder_result: FolderBuildResult,
) -> bool:
    """在命令行确认订单文件夹创建信息。"""
    print("\n[创建文件夹前确认]")
    print(f"平台单号：{platform_order_no}")
    print(f"系统单号：{system_order_no}")
    print(f"文件夹状态：{folder_result.status}")
    print(f"付款时间：{folder_result.payment_time or '-'}")
    print(f"文件夹日期：{folder_result.folder_date or '-'}（来源：{folder_result.folder_date_source or '-'}）")
    if folder_result.folder_name_was_shortened:
        print("\n【重要提示：文件夹名过长，已自动缩短】")
        print("为满足 Windows 文件夹名安全长度，实际目录名删除了部分商品组件。")
        print(f"完整文件夹名（未缩短）：{folder_result.folder_name_full or '-'}")
        print(f"实际创建文件夹名：{folder_result.folder_name or '-'}")
        print("从实际目录名中删除的组件：")
        for index, component in enumerate(folder_result.folder_name_removed_components, start=1):
            print(f"  {index}. {component}")
        if not folder_result.folder_name_removed_components:
            print("  -")
        print("确认创建后，完整名称和删除明细会写入该文件夹内的“完整文件夹名.txt”。")
    else:
        print(f"文件夹名：{folder_result.folder_name or '-'}")
    print(f"完整路径：{folder_result.folder_path or '-'}")
    if folder_result.folder_components:
        print("文件夹组件：")
        for index, component in enumerate(folder_result.folder_components, start=1):
            print(f"  {index}. {component}")
    if folder_result.folder_warnings:
        print(f"警告：{', '.join(folder_result.folder_warnings)}")
    print_folder_rule_missing_details(folder_result)
    answer = await asyncio.to_thread(input, "确认创建该文件夹请输入 y；输入其它内容则不创建且不加入最终完成列表：")
    return answer.strip().lower() in {"y", "yes", "1"}


def folder_rule_missing_lines_from_log(item_result: Mapping[str, Any]) -> list[str]:
    """从批量日志记录中还原缺失规则提示行。"""
    return format_rule_missing_lines(
        status=str(item_result.get("folder_status") or ""),
        title=item_result.get("folder_missing_rule_title"),
        value=item_result.get("folder_missing_rule_value"),
        customization_pairs=item_result.get("customization_pairs") if isinstance(item_result.get("customization_pairs"), Mapping) else None,
        missing_rule_line=item_result.get("folder_missing_rule_line"),
        error=item_result.get("folder_error"),
    )


def format_folder_failure_reason(folder_result: FolderBuildResult) -> str:
    """格式化文件夹流程失败原因，供控制台和日志展示。"""
    status = str(folder_result.status or "folder_failed").strip()
    missing_lines = folder_result.missing_rule_lines()
    if missing_lines:
        return f"{status}（{'；'.join(missing_lines)}）"
    error = " ".join(str(folder_result.error or "").strip().split())
    if error and error != status:
        return f"{status}（{error[:500]}）"
    return status


def print_folder_rule_missing_details(folder_result: FolderBuildResult) -> None:
    """打印文件夹规则缺失的可定位明细。"""
    for line in folder_result.missing_rule_lines():
        print(line)


def notify_existing_folder_in_cmd(
    platform_order_no: str,
    system_order_no: str,
    folder_result: FolderBuildResult,
    *,
    folder_write_enabled: bool = True,
    dedupe_write_enabled: bool = True,
) -> None:
    """在命令行提示已存在订单文件夹及本次处理策略。"""
    print("\n[已有订单文件夹]")
    print(f"平台单号：{platform_order_no}")
    print(f"系统单号：{system_order_no}")
    print(f"文件夹状态：{folder_result.status}")
    print(f"付款时间：{folder_result.payment_time or '-'}")
    print(f"文件夹日期：{folder_result.folder_date or '-'}（来源：{folder_result.folder_date_source or '-'}）")
    print(f"已有路径：{folder_result.folder_path or '-'}")
    if not folder_write_enabled or not dedupe_write_enabled:
        # 安全重测会真实校验订单和写回 ERP，但不会改正式文件夹或正式查重状态。
        print("这是正式文件夹保护，不是查重跳过：本次只识别已有路径，不创建、不覆盖、不复制定制 zip。")
        if not dedupe_write_enabled:
            print("查重写入已关闭：本次不会把订单加入 data/processed_platform_orders.json。")
        print("流程会继续完成安全重测结果统计。")
    else:
        print("当月已存在包含该平台单号的文件夹，本次不重复创建；订单仍可加入最终完成列表。")


def cancel_folder_creation_result(folder_result: FolderBuildResult) -> FolderBuildResult:
    """把文件夹预览结果标记为用户取消创建。"""
    folder_result.status = "folder_creation_cancelled"
    folder_result.error = "用户取消创建文件夹。"
    return folder_result



def finalize_custom_zip_files_for_folder(
    folder_result: FolderBuildResult,
    folder_context: dict[str, Any],
    *,
    allow_folder_write: bool = True,
) -> dict[str, Any]:
    """把已下载的定制 zip 复制到最终订单文件夹。

    安全重测或预览模式会关闭文件夹写入；即使正式目录已经存在，也不能把 zip 复制进去。
    """
    zip_bundle = folder_context.get("zip_bundle")
    zip_files = list(getattr(zip_bundle, "zip_files", []) or [])
    result: dict[str, Any] = {
        "custom_zip_status": CUSTOM_ZIP_SKIPPED_NO_FOLDER,
        "custom_zip_copied_files": [],
        "custom_zip_error": None,
        "custom_zip_staging_cleanup_status": None,
        "custom_zip_staging_cleanup_error": None,
        "full_folder_name_txt": None,
    }
    if not folder_result.folder_path or folder_result.status == "folder_preview":
        return result
    if not allow_folder_write:
        result["custom_zip_error"] = "文件夹写入已关闭，跳过复制定制 zip。"
        return result

    # 完整名称说明属于文件夹创建结果，不应依赖定制 zip 是否成功复制。
    if folder_result.folder_name_was_shortened and folder_result.folder_name_full:
        shorten_result = FolderNameShortenResult(
            full_folder_name=folder_result.folder_name_full,
            safe_folder_name=folder_result.folder_name or folder_result.folder_name_full,
            full_components=folder_result.folder_components_full or folder_result.folder_components,
            safe_components=folder_result.folder_components,
            removed_components=folder_result.folder_name_removed_components,
            was_shortened=folder_result.folder_name_was_shortened,
            max_length=folder_result.folder_name_max_length or 0,
        )
        txt_path = write_full_folder_name_txt(folder_result.folder_path, shorten_result)
        folder_result.full_folder_name_txt = txt_path
        result["full_folder_name_txt"] = txt_path

    if getattr(zip_bundle, "status", None) == CUSTOM_ZIP_DISABLED:
        result["custom_zip_status"] = CUSTOM_ZIP_DISABLED
        return result
    if not zip_files:
        result["custom_zip_error"] = "未下载到定制化 zip 文件。"
        return result

    status, copied_files, error = copy_custom_zip_files_to_folder(zip_files, folder_result.folder_path)
    result["custom_zip_status"] = status
    result["custom_zip_copied_files"] = copied_files
    result["custom_zip_error"] = error
    if status != CUSTOM_ZIP_MOVED:
        return result

    staging_dir = folder_context.get("custom_zip_staging_dir")
    if staging_dir:
        cleanup_status, cleanup_error = cleanup_custom_zip_staging_dir(staging_dir)
        result["custom_zip_staging_cleanup_status"] = cleanup_status
        result["custom_zip_staging_cleanup_error"] = cleanup_error
    return result


def select_customization_text_for_folder(texts: list[str], product_type: str | None) -> str | None:
    """选择最适合生成订单文件夹名的定制化文本。"""
    if product_type == PRODUCT_TYPE_CAR_MAGNET:
        markers = ("Surface Material Option", "Choose Your Magnet Thickness", "Customize Design")
        for text in texts:
            if any(marker.lower() in str(text).lower() for marker in markers):
                return str(text).strip()
    for text in texts:
        if has_supported_contact_prompt(str(text)):
            return str(text).strip()
    return next((str(text).strip() for text in texts if str(text).strip()), None)


def no_contact_car_magnet_contact(texts: list[str]) -> ContactInfo | None:
    """为无联系方式提示的汽车磁贴订单构造可继续建夹的联系信息。"""
    customization_text = select_customization_text_for_folder(texts, PRODUCT_TYPE_CAR_MAGNET)
    if not customization_text:
        return None
    return ContactInfo(
        phone=None,
        email=None,
        source_count=1,
        source_excerpt=normalize_text(customization_text)[:500],
        customization_text=customization_text,
    )

def expected_custom_zip_count(quantity_result: AmazonOrderQuantityResult) -> int | None:
    """根据 Amazon 数量查询结果计算预计应下载的定制化 zip 数量。"""

    if quantity_result.status != AMAZON_QUANTITY_RESOLVED:
        return None
    count = 0
    for order_item in quantity_result.order_items:
        asin = str(order_item.get("asin") or order_item.get("ASIN") or "")
        if match_supported_product(asin):
            count += 1
    return count or None


def expected_custom_zip_order_item_ids(quantity_result: AmazonOrderQuantityResult) -> set[str] | None:
    """从 Amazon 数量结果中提取应下载定制化 zip 的订单行 ID。"""
    if quantity_result.status != AMAZON_QUANTITY_RESOLVED:
        return None
    order_item_ids: set[str] = set()
    for order_item in quantity_result.order_items:
        asin = str(order_item.get("asin") or order_item.get("ASIN") or "")
        if not match_supported_product(asin):
            continue
        order_item_id = str(order_item.get("order_item_id") or order_item.get("OrderItemId") or "").strip()
        if order_item_id:
            order_item_ids.add(order_item_id)
    return order_item_ids or None


def _attachment_download_was_rate_limited(bundle: OrderCustomZipBundle) -> bool:
    """Return whether Lingxing explicitly rejected the attachment read rate."""

    error = str(bundle.error or "").casefold()
    return (
        "3001008" in error
        or "new requests too frequently" in error
        or "requests too frequently" in error
    )


async def collect_order_folder_json_context(
    page,
    item: BatchOrderItem,
    amazon_quantity_client: AmazonOrderQuantityClient,
    system_order_no: str,
    *,
    staging_root: str | Path,
    download_custom_zip: bool,
    api_operations: CustomOrderApiOperations | None = None,
    interaction_policy: CustomOrderInteractionPolicy | None = None,
) -> dict[str, Any]:
    """收集文件夹生成所需的 zip JSON、Amazon 数量和网页详情收件人信息。"""

    context_started = time.monotonic()
    recipient_name, quantity_result = await asyncio.gather(
        read_detail_recipient_name(page),
        amazon_quantity_client.get_order_items(item.platform_order_no),
    )
    recipient_name_source = (
        "lingxing_browser_detail" if recipient_name else "missing"
    )
    if api_operations is not None:
        summary_loader = getattr(amazon_quantity_client, "get_order_summary", None)
        if callable(summary_loader):
            # The lightweight client caches mutable LWA/RDT state; keep the
            # two reads serialized instead of sharing it across worker threads.
            order_summary = await summary_loader(item.platform_order_no)
        else:
            order_summary = None
    else:
        order_summary = None
    identity_context_ms = round((time.monotonic() - context_started) * 1000)
    staging_dir = Path(staging_root) / item.platform_order_no
    if not download_custom_zip:
        zip_bundle = disabled_custom_zip_bundle(item)
        return {
            "recipient_name": recipient_name,
            "recipient_name_source": recipient_name_source,
            "amazon_quantity_result": quantity_result,
            "amazon_order_summary_result": order_summary,
            "zip_bundle": zip_bundle,
            "custom_zip_staging_dir": str(staging_dir),
            "order_lines": [],
            "order_line_warnings": [],
            "order_line_error": "custom_zip_disabled",
            "context_timings": {
                "recipient_and_quantity_ms": identity_context_ms,
                "zip_download_ms": 0,
                "zip_parse_and_match_ms": 0,
            },
        }

    expected_zip_count = expected_custom_zip_count(quantity_result)
    expected_order_item_ids = expected_custom_zip_order_item_ids(quantity_result)
    zip_download_started = time.monotonic()
    if api_operations is not None:
        # Desktop processing is API-only outside contact writeback.  A failed
        # attachment read remains a visible failed stage and is never replayed
        # through the ERP page.
        raw_bundle = await api_operations.download_custom_zip_bundle(
            platform_order_no=item.platform_order_no,
            system_order_no=system_order_no,
            staging_root=staging_root,
            expected_zip_count=expected_zip_count,
            expected_order_item_ids=expected_order_item_ids,
        )
        attachment_rate_limited = _attachment_download_was_rate_limited(
            raw_bundle
        )
        if attachment_rate_limited:
            raw_bundle.warnings.insert(
                0,
                "lingxing_attachment_rate_limited_browser_fallback_skipped",
            )
    else:
        # Frozen CLI compatibility path.  The desktop application always
        # supplies ``api_operations``; this branch is retained only so the
        # script baseline remains recoverable.
        raw_bundle = await download_order_custom_zip_bundle(
            page,
            platform_order_no=item.platform_order_no,
            system_order_no=system_order_no,
            staging_root=staging_root,
            enabled=True,
            expected_zip_count=expected_zip_count,
            expected_order_item_ids=expected_order_item_ids,
        )
    zip_download_ms = round((time.monotonic() - zip_download_started) * 1000)
    zip_parse_started = time.monotonic()
    zip_bundle = parse_order_custom_zip_bundle(raw_bundle, staging_dir) if raw_bundle.status == "ok" else raw_bundle
    order_lines = []
    order_line_warnings: list[str] = []
    order_line_error: str | None = None
    if zip_bundle.status == "ok" and quantity_result.status == AMAZON_QUANTITY_RESOLVED:
        try:
            order_lines, order_line_warnings = build_order_folder_lines_from_json(
                amazon_order_items=quantity_result.order_items,
                customization_items=zip_bundle.customization_items,
            )
        except CustomJsonAmbiguousSameAsinError as exc:
            order_line_error = "custom_json_ambiguous_same_asin: " + str(exc)
        except OrderLineMatchError as exc:
            order_line_error = str(exc)
    zip_parse_and_match_ms = round(
        (time.monotonic() - zip_parse_started) * 1000
    )
    return {
        "recipient_name": recipient_name,
        "recipient_name_source": recipient_name_source,
        "amazon_quantity_result": quantity_result,
        "amazon_order_summary_result": order_summary,
        "zip_bundle": zip_bundle,
        "custom_zip_staging_dir": str(staging_dir),
        "order_lines": order_lines,
        "order_line_warnings": order_line_warnings,
        "order_line_error": order_line_error,
        "context_timings": {
            "recipient_and_quantity_ms": identity_context_ms,
            "zip_download_ms": zip_download_ms,
            "zip_parse_and_match_ms": zip_parse_and_match_ms,
        },
    }




def disabled_custom_zip_bundle(item: BatchOrderItem):
    """构造禁用定制化 zip 下载时的占位结果。"""
    from ..models import OrderCustomZipBundle

    return OrderCustomZipBundle(
        platform_order_no=item.platform_order_no,
        status=CUSTOM_ZIP_DISABLED,
        error="Custom zip download disabled by --no-download-custom-zip.",
    )


def quantity_failure_folder_result(
    quantity_result: AmazonOrderQuantityResult | None,
    *,
    folder_root: str | Path | None,
) -> FolderBuildResult:
    """将 Amazon 数量查询失败转换为文件夹流程的失败结果。"""
    return FolderBuildResult(
        status=str(getattr(quantity_result, "status", None) or "amazon_quantity_error"),
        folder_root=str(folder_root or DEFAULT_FOLDER_ROOT),
        error=str(getattr(quantity_result, "error", "") or "Amazon OrderItems was not resolved."),
    )


def json_context_failure_folder_result(
    folder_context: dict[str, Any],
    *,
    folder_root: str | Path | None,
) -> FolderBuildResult:
    """把 zip / JSON / orderItemId 的失败转换成明确的文件夹失败状态。"""
    quantity_result = folder_context.get("amazon_quantity_result")
    if isinstance(quantity_result, AmazonOrderQuantityResult) and quantity_result.status != AMAZON_QUANTITY_RESOLVED:
        return quantity_failure_folder_result(quantity_result, folder_root=folder_root)
    zip_bundle = folder_context.get("zip_bundle")
    if zip_bundle is not None and getattr(zip_bundle, "status", "ok") != "ok":
        return FolderBuildResult(
            status=str(getattr(zip_bundle, "status", "folder_missing_customization_json")),
            folder_root=str(folder_root or DEFAULT_FOLDER_ROOT),
            error=str(getattr(zip_bundle, "error", "") or "定制化 zip 下载或 JSON 解析失败。"),
        )
    if folder_context.get("order_line_error"):
        error = str(folder_context.get("order_line_error") or "")
        status = "custom_json_ambiguous_same_asin" if error.startswith("custom_json_ambiguous_same_asin") else "folder_missing_customization_json"
        return FolderBuildResult(status=status, folder_root=str(folder_root or DEFAULT_FOLDER_ROOT), error=error)
    return FolderBuildResult(
        status="folder_missing_customization_json",
        folder_root=str(folder_root or DEFAULT_FOLDER_ROOT),
        error="没有可用于生成文件夹的定制化 JSON 商品行。",
    )


def notify_no_contact_writeback_in_cmd(platform_order_no: str, system_order_no: str) -> None:
    """提示该订单没有可写回的联系方式，但仍继续处理文件夹。"""
    print("\n[未填写联系方式]")
    print(f"平台单号：{platform_order_no}")
    print(f"系统单号：{system_order_no}")
    print("定制化信息中没有客户填写的电话/邮箱，本次不写回联系方式；继续生成文件夹并下载定制文件。")


def notify_contact_writeback_already_done_in_cmd(platform_order_no: str, system_order_no: str) -> None:
    """联系方式阶段已持久化完成时，下轮巡检只需要补建文件夹。"""
    print("\n[联系方式已完成]")
    print(f"平台单号：{platform_order_no}")
    print(f"系统单号：{system_order_no}")
    print("联系方式此前已完成，本轮跳过写回，直接补建文件夹。")


def mark_contact_writeback_done(
    dedupe_path: str | Path | None,
    platform_order_no: str,
    system_order_no: str | None,
    *,
    contact_status: str,
    contact_verified: bool = False,
    contact_verification_method: str | None = None,
) -> bool:
    """持久化联系方式阶段完成状态；文件夹失败时下轮可直接补建。"""
    if not dedupe_path:
        return False
    append_contact_writeback_platform_order(
        dedupe_path,
        platform_order_no,
        system_order_no,
        contact_status=contact_status,
        contact_verified=contact_verified,
        contact_verification_method=contact_verification_method,
    )
    return True


def record_contact_writeback_if_allowed(
    dedupe_path: str | Path | None,
    platform_order_no: str,
    system_order_no: str | None,
    *,
    contact_status: str,
    contact_verified: bool = False,
    contact_verification_method: str | None = None,
    write_enabled: bool,
) -> bool:
    """统一控制联系方式阶段状态写入，安全重测时只跑流程、不污染正式查重文件。"""

    if not write_enabled:
        return False
    return mark_contact_writeback_done(
        dedupe_path,
        platform_order_no,
        system_order_no,
        contact_status=contact_status,
        contact_verified=contact_verified,
        contact_verification_method=contact_verification_method,
    )


def append_final_processed_if_allowed(
    dedupe_path: str | Path | None,
    platform_order_no: str,
    system_order_no: str | None,
    *,
    write_enabled: bool,
    product_type: str | None = None,
    sku_adjustment_required: bool = False,
) -> bool:
    """统一控制最终完成状态写入，避免调用方散落判断 --no-dedupe-write。"""

    if not write_enabled or not dedupe_path:
        return False
    append_processed_platform_order(
        dedupe_path,
        platform_order_no,
        system_order_no,
        product_type=product_type,
        sku_adjustment_required=sku_adjustment_required,
    )
    return is_platform_order_processed(dedupe_path, platform_order_no)


def record_folder_complete_if_allowed(
    dedupe_path: str | Path | None,
    platform_order_no: str,
    system_order_no: str | None,
    *,
    write_enabled: bool,
    product_type: str | None,
    sku_adjustment_required: bool,
) -> bool:
    """记录文件夹阶段完成。

    帐篷订单还要继续补 SKU，因此这里只把 folder_complete 记为 true；
    只有 SKU 阶段也完成后，才会被最终查重列表跳过。
    """

    if not write_enabled or not dedupe_path:
        return False
    append_folder_complete_platform_order(
        dedupe_path,
        platform_order_no,
        system_order_no,
        product_type=product_type,
        sku_adjustment_required=sku_adjustment_required,
    )
    return True


def record_sku_adjustment_if_allowed(
    dedupe_path: str | Path | None,
    platform_order_no: str,
    system_order_no: str | None,
    *,
    write_enabled: bool,
    sku_status: str,
    instruction_replaced_at: str | None = None,
    instruction_customer_remark: str | None = None,
    workflow_kind: str | None = None,
) -> bool:
    """记录帐篷 SKU 阶段完成；非帐篷订单不会调用这个函数。"""

    if not write_enabled or not dedupe_path:
        return False
    append_sku_adjustment_platform_order(
        dedupe_path,
        platform_order_no,
        system_order_no,
        sku_status=sku_status,
        instruction_replaced_at=instruction_replaced_at,
        instruction_customer_remark=instruction_customer_remark,
        workflow_kind=workflow_kind,
    )
    return True


def record_package_split_if_allowed(
    dedupe_path: str | Path | None,
    platform_order_no: str,
    system_order_no: str | None,
    *,
    write_enabled: bool,
    package_status: str,
    package_required: bool,
    system_order_nos: list[str] | None = None,
    instruction_remark_required: bool = False,
    warehouse_plan_input: dict[str, Any] | None = None,
    instruction_system_order_no: str | None = None,
) -> bool:
    """记录帐篷拆分包裹阶段完成；所有 JSON 写入都集中在去重存储层。"""

    if not write_enabled or not dedupe_path:
        return False
    append_package_split_platform_order(
        dedupe_path,
        platform_order_no,
        system_order_no,
        package_status=package_status,
        package_required=package_required,
        system_order_nos=system_order_nos,
        instruction_remark_required=instruction_remark_required,
        warehouse_plan_input=warehouse_plan_input,
        instruction_system_order_no=instruction_system_order_no,
    )
    return True


def record_instruction_remark_if_allowed(
    dedupe_path: str | Path | None,
    platform_order_no: str,
    system_order_no: str | None,
    *,
    write_enabled: bool,
    remark_status: str,
    target_system_order_no: str | None,
    warehouse_plan_input: dict[str, Any] | None = None,
) -> bool:
    """记录帐篷说明书客服备注阶段完成。"""

    if not write_enabled or not dedupe_path:
        return False
    append_instruction_remark_platform_order(
        dedupe_path,
        platform_order_no,
        system_order_no,
        remark_status=remark_status,
        target_system_order_no=target_system_order_no,
        warehouse_plan_input=warehouse_plan_input,
    )
    return True


def record_warehouse_logistics_if_allowed(
    dedupe_path: str | Path | None,
    platform_order_no: str,
    system_order_no: str | None,
    *,
    write_enabled: bool,
    warehouse_status: str,
    decisions: list[dict[str, Any]] | None,
    write_results: list[dict[str, Any]] | None,
    result_detail: str | None = None,
    warehouse_required: bool = True,
) -> bool:
    """记录帐篷仓库物流阶段完成。"""

    if not write_enabled or not dedupe_path:
        return False
    append_warehouse_logistics_platform_order(
        dedupe_path,
        platform_order_no,
        system_order_no,
        warehouse_status=warehouse_status,
        decisions=decisions,
        write_results=write_results,
        result_detail=result_detail,
        warehouse_required=warehouse_required,
    )
    return True


def _apply_postal_read_metadata(
    plan: TentSkuAdjustmentPlan,
    *,
    postal_source: str | None,
    postal_error: str | None,
) -> TentSkuAdjustmentPlan:
    """Normalize and attach the audited postal read source to a routing plan."""

    plan.destination.postal_code = normalize_us_postal_code(plan.destination.postal_code)
    plan.destination.postal_source = str(postal_source or "").strip() or None
    plan.destination.postal_error = str(postal_error or "").strip() or None
    return plan


def _warehouse_result_detail(
    plan: TentWarehouseRoutingPlan | None,
    write_results: list[dict[str, Any]] | None,
) -> str:
    """Build a user-facing terminal warehouse outcome without hiding skips."""

    if plan is None:
        return "仓库物流已完成。"
    skip_reasons = list(
        dict.fromkeys(
            str(decision.reason or "").strip()
            for decision in plan.decisions
            if decision.status == "skip" and str(decision.reason or "").strip()
        )
    )
    ready_count = sum(decision.status == "ready" for decision in plan.decisions)
    writes = list(write_results or [])
    already_applied = bool(writes) and all(
        str(item.get("status") or "") == "already_applied" for item in writes
    )
    if not ready_count:
        reason = "；".join(skip_reasons) or str(plan.reason or "").strip() or "规则判定无需修改。"
        return f"无需修改：{reason}"
    if skip_reasons:
        return "仓库物流已完成（部分包裹无需修改）：" + "；".join(skip_reasons)
    if already_applied:
        return "无需修改：当前仓库和物流已经与目标一致。"
    return "仓库物流已完成，所有需要修改的包裹均已写入并读回确认。"


def order_requires_tent_sku_adjustment(
    item: BatchOrderItem,
    order_lines: list[Any],
    *,
    shipping_address_text: str | None = None,
) -> bool:
    """判断订单是否需要进入 SKU/拆单阶段（保留旧函数名兼容调用方）。"""

    if item.product_type == PRODUCT_TYPE_TENT:
        return True
    if any(getattr(line, "product_type", None) == PRODUCT_TYPE_TENT for line in order_lines):
        return True
    return evaluate_high_value_split(
        item,
        order_lines,
        shipping_address_text=shipping_address_text,
    ).requires_stage


def _is_high_value_workflow(item: BatchOrderItem, order_lines: list[Any] | None = None) -> bool:
    return bool(
        item.product_type in NON_TENT_HIGH_VALUE_PRODUCT_TYPES
        or any(
            getattr(line, "product_type", None) in NON_TENT_HIGH_VALUE_PRODUCT_TYPES
            for line in order_lines or []
        )
    )


def _restore_high_value_metadata(
    item: BatchOrderItem,
    *,
    dedupe_path: str | Path | None,
    read_dedupe: bool,
) -> None:
    if not read_dedupe or not dedupe_path:
        return
    record = load_order_workflow_record(dedupe_path, item.platform_order_no) or {}
    item.instruction_replaced_at = (
        item.instruction_replaced_at
        or str(record.get("instruction_replaced_at") or "").strip()
        or None
    )
    item.instruction_customer_remark = (
        item.instruction_customer_remark
        or str(record.get("instruction_customer_remark") or "").strip()
        or None
    )


def _warehouse_plan_input_for_sku_plan(plan: TentSkuAdjustmentPlan) -> dict[str, Any] | None:
    if plan.workflow_kind == HIGH_VALUE_WORKFLOW_KIND:
        return None
    return tent_sku_plan_to_routing_input(plan)


async def confirm_tent_sku_plan_in_cmd(plan) -> bool:
    """在命令行确认帐篷 SKU 调整计划是否继续执行。"""
    print(format_tent_sku_plan_for_cmd(plan))
    answer = await asyncio.to_thread(input, "确认按以上计划调整 SKU 请输入 y；输入其它内容则暂不处理并保留后续补 SKU：")
    return answer.strip().lower() in {"y", "yes", "1"}


async def confirm_manual_tent_sku_done_in_cmd(platform_order_no: str, system_order_no: str, reason: str | None) -> bool:
    """在命令行确认人工 SKU 处理是否已经完成。"""
    print("\n[帐篷 SKU 人工处理]")
    print(f"平台单号：{platform_order_no}")
    print(f"系统单号：{system_order_no}")
    print(f"原因：{reason or '-'}")
    answer = await asyncio.to_thread(input, "如果你已人工添加 SKU，输入 y 标记完成；输入其它内容则后续继续补 SKU：")
    return answer.strip().lower() in {"y", "yes", "1"}


def format_tent_package_split_plan_for_cmd(plan: TentPackageSplitPlan) -> str:
    """把帐篷拆分包裹计划格式化为命令行确认文本。"""

    lines = [
        "\n[帐篷拆分包裹计划]",
        f"平台单号：{plan.platform_order_no}",
        f"系统单号：{plan.system_order_no}",
        f"状态：{plan.status}",
        f"原因：{plan.reason or '-'}",
    ]
    if plan.manual_required:
        lines.append(f"人工原因：{plan.manual_reason or '-'}")
    if plan.packages_to_split:
        lines.append("将主动拆出的包裹：")
        for package in plan.packages_to_split:
            lines.append(f"  - {package.title}：{package.package_key}")
            for item in package.items:
                lines.append(f"    * {item.sku} x{item.quantity}（{item.reason or '按拆包规则'}）")
        lines.append("剩余布料类商品会留在原包裹中。")
    else:
        lines.append("无需主动拆出新包裹。")
    if plan.customer_remark:
        lines.append(f"说明书客服备注：{plan.customer_remark}")
    if plan.warnings:
        lines.append(f"警告：{'；'.join(plan.warnings)}")
    return "\n".join(lines)


async def confirm_tent_package_split_plan_in_cmd(plan: TentPackageSplitPlan) -> bool:
    """在命令行确认帐篷拆分包裹计划是否继续执行。"""

    print(format_tent_package_split_plan_for_cmd(plan))
    answer = await asyncio.to_thread(input, "确认按以上计划拆分包裹请输入 y；输入其它内容则暂不处理并保留后续拆包：")
    return answer.strip().lower() in {"y", "yes", "1"}


async def confirm_manual_tent_package_split_done_in_cmd(plan: TentPackageSplitPlan) -> bool:
    """在命令行确认人工拆包处理是否已经完成或明确无需拆包。"""

    print(format_tent_package_split_plan_for_cmd(plan))
    answer = await asyncio.to_thread(input, "如果你已人工完成拆包或确认无需拆包，输入 y 标记完成；输入其它内容则后续继续拆包：")
    return answer.strip().lower() in {"y", "yes", "1"}


async def refresh_order_list_for_package_split(page, platform_order_no: str, system_order_no: str) -> dict[str, Any]:
    """拆包前刷新订单列表并重新搜索当前平台单号，确保 ERP 使用最新 SKU 数据源。"""

    await close_order_detail_dialog(page)
    try:
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
    except Exception:
        await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
    search_meta = await fill_order_search(page, platform_order_no, "platform")
    system_order_nos = await wait_for_orders_in_list(page, platform_order_no, "platform", 30)
    unique_system_order_nos = list(dict.fromkeys(str(item) for item in system_order_nos if item))
    if system_order_no not in unique_system_order_nos:
        raise RuntimeError(
            f"拆包前刷新后未找到目标系统单号 {system_order_no}；"
            f"平台单号 {platform_order_no} 当前匹配：{unique_system_order_nos or ['无']}。"
        )
    return {
        "package_split_refresh_status": "refreshed",
        "package_split_refresh_search_meta": search_meta,
        "package_split_refresh_system_order_nos": unique_system_order_nos,
    }


def notify_tent_package_split_not_required_in_cmd(plan: TentPackageSplitPlan) -> None:
    """在命令行提示帐篷拆分包裹阶段已判断为无需操作。"""

    print(format_tent_package_split_plan_for_cmd(plan))
    if plan.destination.category in {"canada", "us_non_mainland"}:
        print("拆包判断：加拿大/美国非本土订单不需要拆分包裹，已记录拆分包裹阶段完成。")
        return
    print("拆包判断：当前订单无需进入订单拆分弹窗，已记录拆分包裹阶段完成。")


def tent_instruction_remark_required(plan) -> bool:
    """判断当前帐篷 SKU 计划是否需要在拆包后写说明书客服备注。"""

    replacements = list(getattr(plan, "replace_main_items", None) or [])
    has_instruction = any(
        str(getattr(item, "sku", "") or "").strip().lower() == INSTRUCTION_SKU.lower()
        for item in replacements
    )
    if not replacements:
        has_instruction = (
            str(getattr(plan, "replace_main_sku", "") or "").strip().lower() == INSTRUCTION_SKU.lower()
        )
    return has_instruction and bool(str(getattr(plan, "customer_remark", "") or "").strip())


async def _read_detail_destination_with_web_region(
    page,
    system_order_no: str,
) -> tuple[DetailShippingDestination, str]:
    """Keep API postal metadata while taking country/region from the detail page.

    Lingxing's documented OpenAPI detail payload is not a reliable source for
    the customer destination.  The authenticated ERP detail page is already
    open and exposes the complete ``收件地址`` text, so routing must use that
    text even when the rest of the order context came from OpenAPI.  A cached
    reader avoids evaluating the DOM twice when the internal postal endpoint
    itself falls back to the page.
    """

    web_region_text: str | None = None

    async def read_web_region(current_page) -> str:
        nonlocal web_region_text
        if web_region_text is None:
            try:
                web_region_text = await read_detail_shipping_address_text(
                    current_page
                )
            except Exception:
                # Keep the existing authenticated-detail/API result available
                # when a transient page redraw makes the DOM unreadable.
                web_region_text = ""
        return web_region_text

    destination = await read_detail_shipping_destination(
        page,
        system_order_no,
        dom_reader=read_web_region,
    )
    web_text = str(await read_web_region(page) or "").strip()
    return destination, web_text or destination.shipping_address_text


async def read_shipping_deadline_for_tent_stage(
    page,
    *,
    platform_order_no: str,
    system_order_no: str,
    api_operations: CustomOrderApiOperations | None,
) -> str:
    """Read the documented shipping deadline through API when injected."""

    if api_operations is not None:
        return str(
            await api_operations.get_shipping_deadline_text(
                platform_order_no=platform_order_no,
                system_order_no=system_order_no,
            )
            or ""
        )
    return await read_list_shipping_deadline_text(
        page,
        system_order_no=system_order_no,
        platform_order_no=platform_order_no,
    )


async def run_tent_sku_adjustment_stage(
    page,
    item: BatchOrderItem,
    system_order_no: str,
    folder_result: FolderBuildResult,
    order_lines: list[OrderFolderLine] | None = None,
    *,
    shipping_address_text: str,
    dedupe_path: str | Path | None,
    write_dedupe: bool,
    allow_page_write: bool,
    read_dedupe: bool = True,
    api_operations: CustomOrderApiOperations | None = None,
    interaction_policy: CustomOrderInteractionPolicy | None = None,
) -> dict[str, Any]:
    """
    文件夹完成后执行帐篷 SKU 第三阶段。

    这里仅负责流程编排：读取列表发货时限、让 planner 生成计划、调用 adjuster 执行。
    具体 SKU 规则和 DOM 操作分别在独立模块中维护，避免把页面细节塞进批量主流程。
    """

    payload: dict[str, Any] = {
        "sku_adjustment_required": True,
        "sku_adjustment_complete": False,
        "sku_adjustment_status": None,
        "sku_adjustment_error": None,
        "sku_adjustment_dedupe_read_enabled": read_dedupe,
    }
    if read_dedupe and dedupe_path and is_sku_adjustment_done(dedupe_path, item.platform_order_no):
        payload["sku_adjustment_complete"] = True
        payload["sku_adjustment_status"] = "already_done"
        return payload

    if api_operations is None:
        await close_order_detail_dialog(page)
    high_value_workflow = _is_high_value_workflow(item, order_lines)
    if high_value_workflow:
        _restore_high_value_metadata(
            item,
            dedupe_path=dedupe_path,
            read_dedupe=read_dedupe,
        )
        plan = build_high_value_sku_plan(
            item=item,
            system_order_no=system_order_no,
            order_lines=order_lines,
            shipping_address_text=shipping_address_text,
            processed_at=datetime.now(CHINA_TIMEZONE),
            persisted_customer_remark=item.instruction_customer_remark,
            persisted_replaced_at=item.instruction_replaced_at,
        )
        shipping_deadline_text = ""
        payload["sales_revenue_total"] = item.sales_revenue_total
        payload["sales_revenue_currency"] = item.sales_revenue_currency
        payload["sales_revenue_status"] = item.sales_revenue_status
    else:
        try:
            shipping_deadline_text = await read_shipping_deadline_for_tent_stage(
                page,
                system_order_no=system_order_no,
                platform_order_no=item.platform_order_no,
                api_operations=api_operations,
            )
        except Exception as exc:
            payload["sku_adjustment_status"] = "api_read_failed"
            payload["sku_adjustment_error"] = str(exc)
            return payload
        plan = build_tent_sku_plan(
            platform_order_no=item.platform_order_no,
            system_order_no=system_order_no,
            folder_components=folder_result.folder_components_full or folder_result.folder_components,
            destination_text=shipping_address_text,
            shipping_deadline_text=shipping_deadline_text,
            asin=item.asin,
            payment_time_text=item.paid_at_text,
            logistics_text=item.logistics,
            order_lines=order_lines,
        )
    payload.update(plan.to_log_dict())
    payload["shipping_deadline_text"] = shipping_deadline_text
    payload["sku_adjustment_plan_generated"] = True
    if plan.manual_required:
        manual_confirm = (
            interaction_policy.confirm_manual_sku_done
            if interaction_policy is not None
            else confirm_manual_tent_sku_done_in_cmd
        )
        if await manual_confirm(item.platform_order_no, system_order_no, plan.manual_reason):
            payload["sku_adjustment_status"] = "manual_complete"
            payload["sku_adjustment_complete"] = True
            payload["sku_adjustment_recorded"] = record_sku_adjustment_if_allowed(
                dedupe_path,
                item.platform_order_no,
                system_order_no,
                write_enabled=write_dedupe,
                sku_status="manual",
            )
        else:
            payload["sku_adjustment_status"] = "manual_pending"
            payload["sku_adjustment_error"] = plan.manual_reason
        return payload

    if not allow_page_write:
        payload["sku_adjustment_status"] = "write_disabled"
        payload["sku_adjustment_error"] = "页面写入已关闭，本次只生成 SKU 调整计划。"
        payload["sku_adjustment_plan_only"] = True
        return payload
    plan_confirm = (
        interaction_policy.confirm_sku_plan
        if interaction_policy is not None
        else confirm_tent_sku_plan_in_cmd
    )
    if not await plan_confirm(plan):
        payload["sku_adjustment_status"] = "user_cancelled"
        payload["sku_adjustment_error"] = "用户取消 SKU 调整。"
        return payload

    if not await runtime_write_allowed(
        interaction_policy,
        "sku_adjustment",
        item.platform_order_no,
        system_order_no,
    ):
        mark_runtime_write_blocked(
            payload,
            stage="sku_adjustment",
            stage_label="SKU 调整",
            status_key="sku_adjustment_status",
            error_key="sku_adjustment_error",
        )
        return payload

    if api_operations is not None:
        payload["sku_adjustment_write_source"] = "lingxing_api"
        result = await api_operations.update_tent_skus(
            plan=plan,
            order_lines=list(order_lines or []),
        )
    elif high_value_workflow:
        payload["sku_adjustment_write_source"] = "none"
        payload["sku_adjustment_status"] = "sku_adjustment_api_required"
        payload["sku_adjustment_error"] = (
            "非帐篷换货拆单必须通过领星订单 API 读取实时 local_sku 和数量；"
            "未配置 API 时禁止网页写入。"
        )
        return payload
    else:
        payload["sku_adjustment_write_source"] = "browser"
        result = await execute_tent_sku_adjustment(page, plan)
    payload.update(result.to_log_dict())
    if result.status == "sku_adjustment_complete":
        if high_value_workflow:
            replaced_at = datetime.now(CHINA_TIMEZONE)
            item.instruction_replaced_at = replaced_at.isoformat(timespec="seconds")
            item.instruction_customer_remark = build_processing_instruction_customer_remark(
                processed_at=replaced_at
            )
            plan.instruction_replaced_at = item.instruction_replaced_at
            plan.customer_remark = item.instruction_customer_remark
            payload["instruction_replaced_at"] = item.instruction_replaced_at
            payload["sku_adjustment_customer_remark"] = item.instruction_customer_remark
        payload["sku_adjustment_complete"] = True
        payload["sku_adjustment_recorded"] = record_sku_adjustment_if_allowed(
            dedupe_path,
            item.platform_order_no,
            system_order_no,
            write_enabled=write_dedupe,
            sku_status="auto",
            instruction_replaced_at=item.instruction_replaced_at if high_value_workflow else None,
            instruction_customer_remark=(
                item.instruction_customer_remark if high_value_workflow else None
            ),
            workflow_kind=plan.workflow_kind,
        )
    return payload


def _sku_stage_allows_package_split(
    payload: Mapping[str, Any],
    *,
    package_split_page_write_enabled: bool,
) -> bool:
    if payload.get("sku_adjustment_complete"):
        return True
    return bool(
        package_split_page_write_enabled
        and payload.get("sku_adjustment_status") == "write_disabled"
        and payload.get("sku_adjustment_plan_generated")
    )


async def run_tent_package_split_stage(
    page,
    item: BatchOrderItem,
    system_order_no: str,
    folder_result: FolderBuildResult,
    order_lines: list[OrderFolderLine] | None = None,
    *,
    shipping_address_text: str,
    dedupe_path: str | Path | None,
    write_dedupe: bool,
    allow_page_write: bool,
    shipping_postal_source: str | None = None,
    shipping_postal_error: str | None = None,
    read_dedupe: bool = True,
    api_operations: CustomOrderApiOperations | None = None,
    interaction_policy: CustomOrderInteractionPolicy | None = None,
) -> dict[str, Any]:
    """在帐篷 SKU 完成后执行拆分包裹阶段，并按运行模式决定是否读取阶段去重。"""

    payload: dict[str, Any] = {
        "package_split_required": False,
        "package_split_complete": False,
        "package_split_status": None,
        "package_split_error": None,
        "package_split_dedupe_read_enabled": read_dedupe,
    }
    if read_dedupe and dedupe_path and is_package_split_done(dedupe_path, item.platform_order_no):
        payload["package_split_complete"] = True
        payload["package_split_status"] = "already_done"
        record = load_order_workflow_record(dedupe_path, item.platform_order_no) or {}
        payload["package_split_system_order_nos"] = list(
            record.get("package_split_system_order_nos") or []
        )
        payload["package_split_instruction_system_order_no"] = record.get(
            "package_split_instruction_system_order_no"
        ) or record.get("instruction_remark_target_system_order_no")
        return payload

    if api_operations is None:
        await close_order_detail_dialog(page)
    high_value_workflow = _is_high_value_workflow(item, order_lines)
    if high_value_workflow:
        _restore_high_value_metadata(
            item,
            dedupe_path=dedupe_path,
            read_dedupe=read_dedupe,
        )
        sku_plan = build_high_value_sku_plan(
            item=item,
            system_order_no=system_order_no,
            order_lines=order_lines,
            shipping_address_text=shipping_address_text,
            processed_at=datetime.now(CHINA_TIMEZONE),
            persisted_customer_remark=item.instruction_customer_remark,
            persisted_replaced_at=item.instruction_replaced_at,
        )
        shipping_deadline_text = ""
    else:
        try:
            shipping_deadline_text = await read_shipping_deadline_for_tent_stage(
                page,
                system_order_no=system_order_no,
                platform_order_no=item.platform_order_no,
                api_operations=api_operations,
            )
        except Exception as exc:
            payload["package_split_status"] = "api_read_failed"
            payload["package_split_error"] = str(exc)
            return payload
        sku_plan = _apply_postal_read_metadata(
            build_tent_sku_plan(
                platform_order_no=item.platform_order_no,
                system_order_no=system_order_no,
                folder_components=folder_result.folder_components_full
                or folder_result.folder_components,
                destination_text=shipping_address_text,
                shipping_deadline_text=shipping_deadline_text,
                asin=item.asin,
                payment_time_text=item.paid_at_text,
                logistics_text=item.logistics,
                order_lines=order_lines,
            ),
            postal_source=shipping_postal_source,
            postal_error=shipping_postal_error,
        )
    instruction_remark_required = tent_instruction_remark_required(sku_plan)
    plan = (
        build_high_value_package_split_plan(sku_plan)
        if high_value_workflow
        else build_tent_package_split_plan(sku_plan)
    )
    payload.update(plan.to_log_dict())
    payload["sku_adjustment_workflow_kind"] = sku_plan.workflow_kind
    payload["package_split_shipping_deadline_text"] = shipping_deadline_text
    payload["instruction_remark_required"] = instruction_remark_required
    split_confirm = (
        interaction_policy.confirm_package_split_plan
        if interaction_policy is not None
        else confirm_tent_package_split_plan_in_cmd
    )

    if plan.manual_required:
        manual_confirm = (
            interaction_policy.confirm_manual_package_split_done
            if interaction_policy is not None
            else confirm_manual_tent_package_split_done_in_cmd
        )
        if await manual_confirm(plan):
            payload["package_split_status"] = "manual_complete"
            payload["package_split_complete"] = True
            payload["instruction_remark_confirmation_granted"] = True
            payload["package_split_recorded"] = record_package_split_if_allowed(
                dedupe_path,
                item.platform_order_no,
                system_order_no,
                write_enabled=write_dedupe,
                package_status="manual",
                package_required=plan.required,
                system_order_nos=[],
                instruction_remark_required=instruction_remark_required,
                warehouse_plan_input=_warehouse_plan_input_for_sku_plan(sku_plan),
            )
        else:
            payload["package_split_status"] = "manual_pending"
            payload["package_split_error"] = plan.manual_reason
        return payload

    if not plan.required:
        if instruction_remark_required and allow_page_write:
            if not await split_confirm(plan):
                payload["instruction_remark_confirmation_granted"] = False
                payload["instruction_remark_confirmation_error"] = "用户取消说明书备注写入。"
            else:
                payload["instruction_remark_confirmation_granted"] = True
        notify_tent_package_split_not_required_in_cmd(plan)
        payload["package_split_status"] = plan.status
        payload["package_split_complete"] = True
        payload["package_split_recorded"] = record_package_split_if_allowed(
            dedupe_path,
            item.platform_order_no,
            system_order_no,
            write_enabled=write_dedupe,
            package_status=plan.status,
            package_required=False,
            system_order_nos=[],
            instruction_remark_required=instruction_remark_required,
            warehouse_plan_input=_warehouse_plan_input_for_sku_plan(sku_plan),
        )
        return payload

    if not allow_page_write:
        payload["package_split_status"] = "write_disabled"
        payload["package_split_error"] = "页面拆包写入已关闭，本次只生成拆包计划。"
        return payload
    if not await split_confirm(plan):
        payload["package_split_status"] = "user_cancelled"
        payload["package_split_error"] = "用户取消拆分包裹。"
        return payload
    payload["instruction_remark_confirmation_granted"] = True

    if api_operations is None:
        try:
            payload.update(
                await refresh_order_list_for_package_split(
                    page,
                    item.platform_order_no,
                    system_order_no,
                )
            )
        except Exception as exc:
            payload["package_split_status"] = "refresh_failed"
            payload["package_split_error"] = str(exc)
            return payload
        if not await runtime_write_allowed(
            interaction_policy,
            "package_split",
            item.platform_order_no,
            system_order_no,
        ):
            mark_runtime_write_blocked(
                payload,
                stage="package_split",
                stage_label="拆分包裹",
                status_key="package_split_status",
                error_key="package_split_error",
            )
            return payload
        payload["package_split_write_source"] = "browser"
        result = await execute_tent_package_split(page, plan)
    else:
        if not await runtime_write_allowed(
            interaction_policy,
            "package_split",
            item.platform_order_no,
            system_order_no,
        ):
            mark_runtime_write_blocked(
                payload,
                stage="package_split",
                stage_label="拆分包裹",
                status_key="package_split_status",
                error_key="package_split_error",
            )
            return payload
        payload["package_split_refresh_status"] = "api_snapshot"
        payload["package_split_write_source"] = "lingxing_api"
        result = await api_operations.split_tent_packages(plan=plan)
    payload.update(result.to_log_dict())
    if result.status == "package_split_complete":
        payload["package_split_complete"] = True
        payload["package_split_recorded"] = record_package_split_if_allowed(
            dedupe_path,
            item.platform_order_no,
            system_order_no,
            write_enabled=write_dedupe,
            package_status="auto",
            package_required=True,
            system_order_nos=result.system_order_nos,
            instruction_remark_required=instruction_remark_required,
            warehouse_plan_input=_warehouse_plan_input_for_sku_plan(sku_plan),
            instruction_system_order_no=result.instruction_system_order_no,
        )
    return payload


async def run_tent_instruction_remark_stage(
    page,
    item: BatchOrderItem,
    system_order_no: str,
    folder_result: FolderBuildResult,
    order_lines: list[OrderFolderLine] | None = None,
    *,
    shipping_address_text: str,
    package_split_system_order_nos: list[str] | None,
    package_split_instruction_system_order_no: str | None = None,
    instruction_remark_confirmation_granted: bool | None = None,
    dedupe_path: str | Path | None,
    write_dedupe: bool,
    allow_page_write: bool,
    shipping_postal_source: str | None = None,
    shipping_postal_error: str | None = None,
    read_dedupe: bool = True,
    api_operations: CustomOrderApiOperations | None = None,
    interaction_policy: CustomOrderInteractionPolicy | None = None,
) -> dict[str, Any]:
    """拆包完成后，把说明书客服备注只写到包含 Instruction 的系统订单行。"""

    payload: dict[str, Any] = {
        "instruction_remark_required": False,
        "instruction_remark_complete": False,
        "instruction_remark_status": None,
        "instruction_remark_error": None,
        "instruction_remark_dedupe_read_enabled": read_dedupe,
    }

    if api_operations is None:
        await close_order_detail_dialog(page)
    high_value_workflow = _is_high_value_workflow(item, order_lines)
    sku_plan_override: TentSkuAdjustmentPlan | None = None
    if high_value_workflow:
        _restore_high_value_metadata(
            item,
            dedupe_path=dedupe_path,
            read_dedupe=read_dedupe,
        )
        sku_plan_override = build_high_value_sku_plan(
            item=item,
            system_order_no=system_order_no,
            order_lines=order_lines,
            shipping_address_text=shipping_address_text,
            processed_at=datetime.now(CHINA_TIMEZONE),
            persisted_customer_remark=item.instruction_customer_remark,
            persisted_replaced_at=item.instruction_replaced_at,
        )
        shipping_deadline_text = ""
    else:
        try:
            shipping_deadline_text = await read_shipping_deadline_for_tent_stage(
                page,
                system_order_no=system_order_no,
                platform_order_no=item.platform_order_no,
                api_operations=api_operations,
            )
        except Exception as exc:
            payload["instruction_remark_status"] = "api_read_failed"
            payload["instruction_remark_error"] = str(exc)
            return payload
    result_payload = await _continue_tent_instruction_remark_stage(
        page=page,
        item=item,
        system_order_no=system_order_no,
        folder_result=folder_result,
        order_lines=order_lines,
        shipping_address_text=shipping_address_text,
        shipping_postal_source=shipping_postal_source,
        shipping_postal_error=shipping_postal_error,
        shipping_deadline_text=shipping_deadline_text,
        package_split_system_order_nos=package_split_system_order_nos,
        package_split_instruction_system_order_no=package_split_instruction_system_order_no,
        instruction_remark_confirmation_granted=instruction_remark_confirmation_granted,
        dedupe_path=dedupe_path,
        write_dedupe=write_dedupe,
        allow_page_write=allow_page_write,
        read_dedupe=read_dedupe,
        api_operations=api_operations,
        interaction_policy=interaction_policy,
        payload=payload,
        sku_plan_override=sku_plan_override,
    )
    if high_value_workflow and result_payload.get("instruction_remark_complete"):
        result_payload["warehouse_logistics_required"] = False
        result_payload["warehouse_logistics_complete"] = True
        result_payload["warehouse_logistics_status"] = "not_required_non_tent"
        result_payload["warehouse_logistics_recorded"] = record_warehouse_logistics_if_allowed(
            dedupe_path,
            item.platform_order_no,
            system_order_no,
            write_enabled=write_dedupe,
            warehouse_status="not_required_non_tent",
            decisions=[],
            write_results=[],
            result_detail="非帐篷订单不处理仓库物流；金额达到 200 USD/CAD 时仅执行高金额换货拆单流程。",
            warehouse_required=False,
        )
    return result_payload


async def run_persisted_high_value_instruction_remark_stage(
    page,
    item: BatchOrderItem,
    *,
    workflow_record: Mapping[str, Any],
    candidate_system_order_nos: list[str],
    dedupe_path: str | Path | None,
    write_dedupe: bool,
    allow_page_write: bool,
    read_dedupe: bool,
    api_operations: CustomOrderApiOperations | None,
    interaction_policy: CustomOrderInteractionPolicy | None,
) -> dict[str, Any]:
    """拆单后重启时，使用已持久化的换货时间/备注继续写 Instruction 订单备注。"""

    remark = str(workflow_record.get("instruction_customer_remark") or "").strip()
    replaced_at = str(workflow_record.get("instruction_replaced_at") or "").strip()
    payload: dict[str, Any] = {
        "instruction_remark_required": True,
        "instruction_remark_complete": False,
        "instruction_remark_status": None,
        "instruction_remark_error": None,
        "instruction_remark_dedupe_read_enabled": read_dedupe,
        "instruction_replaced_at": replaced_at or None,
        "instruction_remark_customer_remark": remark or None,
        "sku_adjustment_workflow_kind": HIGH_VALUE_WORKFLOW_KIND,
    }
    if not remark or not replaced_at:
        payload["instruction_remark_status"] = "instruction_remark_manual_review"
        payload["instruction_remark_error"] = "缺少已持久化的实际换货时间或说明书备注，禁止按重试日期重算。"
        return payload
    plan = TentSkuAdjustmentPlan(
        platform_order_no=item.platform_order_no,
        system_order_no=str(workflow_record.get("system_order_no") or item.system_order_no or ""),
        destination=parse_destination_region("United States"),
        replace_main_items=[
            TentSkuPlanAction(action="replace_main", sku=INSTRUCTION_SKU, quantity=1)
        ],
        customer_remark=remark,
        workflow_kind=HIGH_VALUE_WORKFLOW_KIND,
        instruction_replaced_at=replaced_at,
    )
    result_payload = await _continue_tent_instruction_remark_stage(
        page=page,
        item=item,
        system_order_no=plan.system_order_no,
        folder_result=None,  # sku_plan_override makes folder data unnecessary.
        order_lines=None,
        shipping_address_text="",
        shipping_postal_source=None,
        shipping_postal_error=None,
        shipping_deadline_text="",
        package_split_system_order_nos=candidate_system_order_nos,
        package_split_instruction_system_order_no=str(
            workflow_record.get("package_split_instruction_system_order_no")
            or workflow_record.get("instruction_remark_target_system_order_no")
            or ""
        ).strip()
        or None,
        instruction_remark_confirmation_granted=True,
        dedupe_path=dedupe_path,
        write_dedupe=write_dedupe,
        allow_page_write=allow_page_write,
        read_dedupe=read_dedupe,
        api_operations=api_operations,
        interaction_policy=interaction_policy,
        payload=payload,
        sku_plan_override=plan,
    )
    if result_payload.get("instruction_remark_complete"):
        result_payload["warehouse_logistics_required"] = False
        result_payload["warehouse_logistics_complete"] = True
        result_payload["warehouse_logistics_status"] = "not_required_non_tent"
        result_payload["warehouse_logistics_recorded"] = record_warehouse_logistics_if_allowed(
            dedupe_path,
            item.platform_order_no,
            plan.system_order_no,
            write_enabled=write_dedupe,
            warehouse_status="not_required_non_tent",
            decisions=[],
            write_results=[],
            result_detail="非帐篷订单不处理仓库物流；金额达到 200 USD/CAD 时仅执行高金额换货拆单流程。",
            warehouse_required=False,
        )
    return result_payload


def format_tent_warehouse_routing_plan_for_cmd(plan: TentWarehouseRoutingPlan) -> str:
    """把拆单后仓库物流计划格式化为确认文本。"""

    lines = [
        "\n[帐篷仓库物流计划]",
        f"平台单号：{plan.platform_order_no}",
        f"邮编：{plan.postal_code or '-'}",
        f"状态：{plan.status}",
        f"规则版本：v{plan.schema_version} / {plan.source_sha256[:12] or '-'}",
        f"原因：{plan.reason or '-'}",
    ]
    for decision in plan.decisions:
        target = (
            f"{decision.target_warehouse_name} / {decision.target_channel_name}"
            if decision.target_warehouse_name
            else "保持不变"
        )
        lines.append(
            f"  - {decision.system_order_no}：{', '.join(decision.skus) or '-'} → {target}"
        )
        lines.append(f"    {decision.reason}")
    return "\n".join(lines)


def _routing_packages_from_payload(
    value: Any,
) -> tuple[TentRoutingPackage, ...]:
    """Validate the split acknowledgement projection before previewing it."""

    if not isinstance(value, list):
        return ()
    packages: list[TentRoutingPackage] = []
    for raw_package in value:
        if not isinstance(raw_package, Mapping):
            return ()
        system_order_no = str(raw_package.get("system_order_no") or "").strip()
        raw_items = raw_package.get("items")
        if not system_order_no or not isinstance(raw_items, list) or not raw_items:
            return ()
        items: list[TentRoutingItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                return ()
            sku = str(raw_item.get("sku") or "").strip()
            try:
                quantity = int(raw_item.get("quantity") or 0)
            except (TypeError, ValueError):
                return ()
            if not sku or quantity <= 0:
                return ()
            items.append(
                TentRoutingItem(
                    sku=sku,
                    quantity=quantity,
                    item_id=str(raw_item.get("item_id") or "").strip() or None,
                    order_item_no=(
                        str(raw_item.get("order_item_no") or "").strip() or None
                    ),
                )
            )
        packages.append(
            TentRoutingPackage(
                system_order_no=system_order_no,
                items=tuple(items),
            )
        )
    return tuple(packages)


def _warehouse_plan_fingerprint(plan: TentWarehouseRoutingPlan) -> str:
    return json.dumps(
        plan.to_log_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def confirm_tent_warehouse_routing_plan_in_cmd(
    plan: TentWarehouseRoutingPlan,
) -> bool:
    print(format_tent_warehouse_routing_plan_for_cmd(plan))
    answer = await asyncio.to_thread(
        input,
        "确认按以上计划设置仓库物流请输入 y；输入其它内容则保留待处理：",
    )
    return answer.strip().lower() in {"y", "yes", "1"}


async def run_tent_warehouse_logistics_stage(
    page,
    item: BatchOrderItem,
    system_order_no: str,
    folder_result: FolderBuildResult | None,
    order_lines: list[OrderFolderLine] | None = None,
    *,
    shipping_address_text: str = "",
    package_split_system_order_nos: list[str] | None,
    dedupe_path: str | Path | None,
    write_dedupe: bool,
    allow_page_write: bool,
    shipping_postal_source: str | None = None,
    shipping_postal_error: str | None = None,
    read_dedupe: bool = True,
    api_operations: CustomOrderApiOperations | None = None,
    interaction_policy: CustomOrderInteractionPolicy | None = None,
    sku_plan_override: TentSkuAdjustmentPlan | None = None,
    runtime_system_order_no: str | None = None,
    package_split_projected_packages: Any = None,
) -> dict[str, Any]:
    """在拆包和说明书阶段完成后，规划并设置各系统单的仓库物流。"""

    payload: dict[str, Any] = {
        "warehouse_logistics_required": True,
        "warehouse_logistics_complete": False,
        "warehouse_logistics_status": None,
        "warehouse_logistics_error": None,
        "warehouse_logistics_result_detail": None,
        "warehouse_logistics_dedupe_read_enabled": read_dedupe,
        "warehouse_logistics_decisions": [],
        "warehouse_logistics_write_results": [],
    }
    if read_dedupe and dedupe_path and is_warehouse_logistics_done(
        dedupe_path, item.platform_order_no
    ):
        payload["warehouse_logistics_complete"] = True
        payload["warehouse_logistics_status"] = "already_done"
        return payload
    if api_operations is None:
        payload["warehouse_logistics_status"] = "api_unavailable"
        payload["warehouse_logistics_error"] = (
            "仓库物流阶段只允许使用领星 API；未配置 API 时不会网页兜底。"
        )
        return payload

    sku_plan = sku_plan_override
    if sku_plan is None:
        if folder_result is None:
            payload["warehouse_logistics_status"] = "plan_input_missing"
            payload["warehouse_logistics_error"] = "缺少帐篷 SKU 计划，无法安全识别主商品行。"
            return payload
        try:
            shipping_deadline_text = await read_shipping_deadline_for_tent_stage(
                page,
                system_order_no=system_order_no,
                platform_order_no=item.platform_order_no,
                api_operations=api_operations,
            )
        except Exception as exc:
            payload["warehouse_logistics_status"] = "api_read_failed"
            payload["warehouse_logistics_error"] = str(exc)
            return payload
        sku_plan = _apply_postal_read_metadata(
            build_tent_sku_plan(
                platform_order_no=item.platform_order_no,
                system_order_no=system_order_no,
                folder_components=folder_result.folder_components_full
                or folder_result.folder_components,
                destination_text=shipping_address_text,
                shipping_deadline_text=shipping_deadline_text,
                asin=item.asin,
                payment_time_text=item.paid_at_text,
                logistics_text=item.logistics,
                order_lines=order_lines,
            ),
            postal_source=shipping_postal_source,
            postal_error=shipping_postal_error,
        )

    candidates = list(
        dict.fromkeys(
            str(value).strip()
            for value in (package_split_system_order_nos or [system_order_no])
            if str(value).strip()
        )
    )
    projected_packages = _routing_packages_from_payload(
        package_split_projected_packages
    )
    preview_started = time.monotonic()
    try:
        preview = await api_operations.set_tent_warehouse_logistics(
            plan=sku_plan,
            candidate_system_order_nos=candidates,
            apply=False,
            projected_packages=projected_packages or None,
        )
    except Exception as exc:
        payload["warehouse_logistics_status"] = "api_preview_failed"
        payload["warehouse_logistics_error"] = str(exc)
        return payload
    payload["warehouse_logistics_preview_ms"] = round(
        (time.monotonic() - preview_started) * 1000
    )
    payload["warehouse_logistics_projection_source"] = str(
        preview.details.get("projection_source") or "order_list"
    )
    payload["warehouse_logistics_preview_status"] = preview.status
    payload["warehouse_logistics_request_id"] = preview.request_id
    if preview.plan is not None:
        payload.update(preview.plan.to_log_dict())
        payload["warehouse_logistics_decisions"] = [
            decision.to_log_dict() for decision in preview.plan.decisions
        ]
    payload["warehouse_logistics_write_results"] = list(
        preview.details.get("writes") or []
    )
    if preview.status == "succeeded":
        payload["warehouse_logistics_complete"] = True
        payload["warehouse_logistics_status"] = (
            preview.plan.status if preview.plan is not None else "not_required"
        )
        payload["warehouse_logistics_result_detail"] = _warehouse_result_detail(
            preview.plan,
            payload["warehouse_logistics_write_results"],
        )
        payload["warehouse_logistics_recorded"] = record_warehouse_logistics_if_allowed(
            dedupe_path,
            item.platform_order_no,
            system_order_no,
            write_enabled=write_dedupe,
            warehouse_status=str(payload["warehouse_logistics_status"]),
            decisions=payload["warehouse_logistics_decisions"],
            write_results=payload["warehouse_logistics_write_results"],
            result_detail=payload["warehouse_logistics_result_detail"],
        )
        return payload
    if preview.status != "preview" or preview.plan is None:
        payload["warehouse_logistics_status"] = (
            "warehouse_logistics_manual_review"
            if preview.manual_review_required
            else "warehouse_logistics_api_failed"
        )
        payload["warehouse_logistics_error"] = preview.message
        return payload
    if not allow_page_write:
        payload["warehouse_logistics_status"] = "write_disabled"
        payload["warehouse_logistics_error"] = (
            "仓库物流写入已关闭，本次只输出每个系统单的邮编、SKU、目标仓库和渠道。"
        )
        return payload

    authoritative_preview_task = (
        asyncio.create_task(
            api_operations.set_tent_warehouse_logistics(
                plan=sku_plan,
                candidate_system_order_nos=candidates,
                apply=False,
                projected_packages=None,
            )
        )
        if projected_packages
        else None
    )
    confirm = (
        getattr(interaction_policy, "confirm_warehouse_logistics_plan", None)
        if interaction_policy is not None
        and getattr(interaction_policy, "confirm_warehouse_logistics_plan", None) is not None
        else confirm_tent_warehouse_routing_plan_in_cmd
    )
    try:
        approved = await confirm(preview.plan)
    except BaseException:
        if authoritative_preview_task is not None:
            authoritative_preview_task.cancel()
            try:
                await authoritative_preview_task
            except asyncio.CancelledError:
                pass
        raise
    if not approved:
        if authoritative_preview_task is not None:
            authoritative_preview_task.cancel()
            try:
                await authoritative_preview_task
            except asyncio.CancelledError:
                pass
        payload["warehouse_logistics_status"] = "user_cancelled"
        payload["warehouse_logistics_error"] = "用户取消设置帐篷仓库物流。"
        return payload

    if authoritative_preview_task is not None:
        projection_wait_started = time.monotonic()
        try:
            authoritative_preview = await authoritative_preview_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            payload["warehouse_logistics_status"] = "api_preview_failed"
            payload["warehouse_logistics_error"] = str(exc)
            return payload
        payload["warehouse_logistics_projection_wait_after_approval_ms"] = round(
            (time.monotonic() - projection_wait_started) * 1000
        )
        payload["warehouse_logistics_projection_attempts"] = int(
            authoritative_preview.details.get("projection_attempts") or 0
        )
        payload["warehouse_logistics_projection_waited_seconds"] = float(
            authoritative_preview.details.get("projection_waited_seconds") or 0.0
        )
        if (
            authoritative_preview.status not in {"preview", "succeeded"}
            or authoritative_preview.plan is None
        ):
            payload["warehouse_logistics_status"] = (
                "warehouse_logistics_manual_review"
                if authoritative_preview.manual_review_required
                else "warehouse_logistics_api_failed"
            )
            payload["warehouse_logistics_error"] = authoritative_preview.message
            return payload
        if authoritative_preview.status == "succeeded":
            payload.update(authoritative_preview.plan.to_log_dict())
            payload["warehouse_logistics_decisions"] = [
                decision.to_log_dict()
                for decision in authoritative_preview.plan.decisions
            ]
            payload["warehouse_logistics_complete"] = True
            payload["warehouse_logistics_status"] = (
                authoritative_preview.plan.status
            )
            payload["warehouse_logistics_result_detail"] = (
                _warehouse_result_detail(authoritative_preview.plan, [])
            )
            payload["warehouse_logistics_recorded"] = (
                record_warehouse_logistics_if_allowed(
                    dedupe_path,
                    item.platform_order_no,
                    system_order_no,
                    write_enabled=write_dedupe,
                    warehouse_status=str(payload["warehouse_logistics_status"]),
                    decisions=payload["warehouse_logistics_decisions"],
                    write_results=[],
                    result_detail=payload["warehouse_logistics_result_detail"],
                )
            )
            return payload
        if _warehouse_plan_fingerprint(
            authoritative_preview.plan
        ) != _warehouse_plan_fingerprint(preview.plan):
            payload["warehouse_logistics_plan_changed_after_sync"] = True
            payload.update(authoritative_preview.plan.to_log_dict())
            payload["warehouse_logistics_decisions"] = [
                decision.to_log_dict()
                for decision in authoritative_preview.plan.decisions
            ]
            if not await confirm(authoritative_preview.plan):
                payload["warehouse_logistics_status"] = "user_cancelled"
                payload["warehouse_logistics_error"] = (
                    "领星子单同步后的仓库物流方案发生变化，用户未确认新方案。"
                )
                return payload
        preview = authoritative_preview

    if not await runtime_write_allowed(
        interaction_policy,
        "warehouse_logistics",
        item.platform_order_no,
        runtime_system_order_no or system_order_no,
    ):
        mark_runtime_write_blocked(
            payload,
            stage="warehouse_logistics",
            stage_label="仓库物流",
            status_key="warehouse_logistics_status",
            error_key="warehouse_logistics_error",
        )
        return payload

    try:
        outcome = await api_operations.set_tent_warehouse_logistics(
            plan=sku_plan,
            candidate_system_order_nos=candidates,
            apply=True,
        )
    except Exception as exc:
        payload["warehouse_logistics_status"] = "warehouse_logistics_api_failed"
        payload["warehouse_logistics_error"] = str(exc)
        return payload
    payload["warehouse_logistics_request_id"] = outcome.request_id
    payload["warehouse_logistics_write_results"] = list(
        outcome.details.get("writes") or []
    )
    if outcome.plan is not None:
        payload["warehouse_logistics_decisions"] = [
            decision.to_log_dict() for decision in outcome.plan.decisions
        ]
    if not outcome.succeeded:
        payload["warehouse_logistics_status"] = (
            "warehouse_logistics_manual_review"
            if outcome.manual_review_required
            else "warehouse_logistics_api_failed"
        )
        payload["warehouse_logistics_error"] = outcome.message
        return payload
    payload["warehouse_logistics_complete"] = True
    payload["warehouse_logistics_status"] = "warehouse_logistics_complete"
    payload["warehouse_logistics_result_detail"] = _warehouse_result_detail(
        outcome.plan,
        payload["warehouse_logistics_write_results"],
    )
    payload["warehouse_logistics_recorded"] = record_warehouse_logistics_if_allowed(
        dedupe_path,
        item.platform_order_no,
        system_order_no,
        write_enabled=write_dedupe,
        warehouse_status="auto",
        decisions=payload["warehouse_logistics_decisions"],
        write_results=payload["warehouse_logistics_write_results"],
        result_detail=payload["warehouse_logistics_result_detail"],
    )
    return payload


async def _continue_tent_instruction_remark_stage(
    *,
    page,
    item: BatchOrderItem,
    system_order_no: str,
    folder_result: FolderBuildResult,
    order_lines: list[OrderFolderLine] | None,
    shipping_address_text: str,
    shipping_postal_source: str | None,
    shipping_postal_error: str | None,
    shipping_deadline_text: str | None,
    package_split_system_order_nos: list[str] | None,
    package_split_instruction_system_order_no: str | None,
    instruction_remark_confirmation_granted: bool | None,
    dedupe_path: str | Path | None,
    write_dedupe: bool,
    allow_page_write: bool,
    read_dedupe: bool,
    api_operations: CustomOrderApiOperations | None,
    interaction_policy: CustomOrderInteractionPolicy | None,
    payload: dict[str, Any],
    sku_plan_override: TentSkuAdjustmentPlan | None = None,
) -> dict[str, Any]:
    sku_plan = sku_plan_override or _apply_postal_read_metadata(
        build_tent_sku_plan(
            platform_order_no=item.platform_order_no,
            system_order_no=system_order_no,
            folder_components=folder_result.folder_components_full
            or folder_result.folder_components,
            destination_text=shipping_address_text,
            shipping_deadline_text=shipping_deadline_text,
            asin=item.asin,
            payment_time_text=item.paid_at_text,
            logistics_text=item.logistics,
            order_lines=order_lines,
        ),
        postal_source=shipping_postal_source,
        postal_error=shipping_postal_error,
    )
    required = tent_instruction_remark_required(sku_plan)
    payload["instruction_remark_required"] = required
    payload["instruction_remark_customer_remark"] = sku_plan.customer_remark
    payload["instruction_remark_shipping_deadline_text"] = shipping_deadline_text
    if not required:
        payload["instruction_remark_complete"] = True
        payload["instruction_remark_status"] = "not_required"
        return payload

    if read_dedupe and dedupe_path and is_instruction_remark_done(dedupe_path, item.platform_order_no):
        payload["instruction_remark_complete"] = True
        payload["instruction_remark_status"] = "already_done"
        return payload

    if not allow_page_write:
        payload["instruction_remark_status"] = "write_disabled"
        payload["instruction_remark_error"] = "页面写入已关闭，本次只生成说明书备注计划。"
        return payload

    if instruction_remark_confirmation_granted is False:
        payload["instruction_remark_status"] = "user_cancelled"
        payload["instruction_remark_error"] = "用户已在拆包及说明书备注确认中取消备注写入。"
        return payload

    if not await runtime_write_allowed(
        interaction_policy,
        "instruction_remark",
        item.platform_order_no,
        system_order_no,
    ):
        mark_runtime_write_blocked(
            payload,
            stage="instruction_remark",
            stage_label="说明书备注",
            status_key="instruction_remark_status",
            error_key="instruction_remark_error",
        )
        return payload

    try:
        if api_operations is not None:
            outcome = await api_operations.set_instruction_remark(
                platform_order_no=item.platform_order_no,
                candidate_system_order_nos=[
                    str(value).strip()
                    for value in package_split_system_order_nos or []
                    if str(value).strip()
                ],
                remark=str(sku_plan.customer_remark or ""),
                target_system_order_no=package_split_instruction_system_order_no,
            )
            payload["instruction_remark_write_source"] = "lingxing_api"
            payload["instruction_remark_request_id"] = outcome.request_id
            if not outcome.succeeded:
                payload["instruction_remark_status"] = (
                    "instruction_remark_manual_review"
                    if outcome.manual_review_required
                    else "instruction_remark_api_failed"
                )
                payload["instruction_remark_error"] = outcome.message
                return payload
            action = outcome.action or "api"
            target_system_order_no = outcome.target_system_order_no
            payload["instruction_remark_complete"] = True
            payload["instruction_remark_status"] = "instruction_remark_complete"
            payload["instruction_remark_action"] = action
            payload["instruction_remark_target_system_order_no"] = target_system_order_no
            payload["instruction_remark_recorded"] = record_instruction_remark_if_allowed(
                dedupe_path,
                item.platform_order_no,
                system_order_no,
                write_enabled=write_dedupe,
                remark_status=action,
                target_system_order_no=target_system_order_no,
                warehouse_plan_input=_warehouse_plan_input_for_sku_plan(sku_plan),
            )
            return payload

        target_system_order_no = str(package_split_instruction_system_order_no or "").strip() or next(
            (str(value).strip() for value in package_split_system_order_nos or [] if str(value).strip()),
            None,
        )
        if not target_system_order_no:
            payload["instruction_remark_status"] = "instruction_remark_error"
            payload["instruction_remark_error"] = "拆包成功弹窗没有返回说明书备注目标系统单号。"
            return payload

        await close_order_detail_dialog(page)
        payload["instruction_remark_write_source"] = "browser"
        action = await upsert_instruction_customer_remark(
            page,
            platform_order_no=item.platform_order_no,
            system_order_no=target_system_order_no,
            remark=str(sku_plan.customer_remark or ""),
        )
        payload["instruction_remark_complete"] = True
        payload["instruction_remark_status"] = "instruction_remark_complete"
        payload["instruction_remark_action"] = action
        payload["instruction_remark_target_system_order_no"] = target_system_order_no
        payload["instruction_remark_recorded"] = record_instruction_remark_if_allowed(
            dedupe_path,
            item.platform_order_no,
            system_order_no,
            write_enabled=write_dedupe,
            remark_status=action,
            target_system_order_no=target_system_order_no,
            warehouse_plan_input=_warehouse_plan_input_for_sku_plan(sku_plan),
        )
        return payload
    except Exception as exc:
        if api_operations is None:
            await close_order_detail_dialog(page)
        payload["instruction_remark_status"] = "instruction_remark_error"
        payload["instruction_remark_error"] = str(exc)
        return payload


def append_runtime_safety_notes(
    message: str,
    *,
    folder_write_enabled: bool,
    dedupe_write_enabled: bool,
) -> str:
    """在结果说明中明确本次安全开关，避免把预览误解为正式完成。"""

    notes: list[str] = []
    if not folder_write_enabled:
        notes.append("文件夹写入已关闭：只预览路径，不创建/写入 Z 盘，也不复制定制 zip。")
    if not dedupe_write_enabled:
        notes.append("本次未写入查重状态文件。")
    return f"{message} {' '.join(notes)}" if notes else message


FORMAL_COMPLETION_PHRASE = "文件夹和定制文件已完成，已加入最终完成列表，后续不再巡检。"
FORMAL_COMPLETION_SHORT_PHRASE = "文件夹和定制文件已完成，已加入最终完成列表。"
FORMAL_TENT_SKU_COMPLETION_PHRASE = "联系方式、文件夹、定制文件和帐篷 SKU 均已完成，已加入最终完成列表。"
FORMAL_TENT_PACKAGE_SPLIT_COMPLETION_PHRASE = "联系方式、文件夹、定制文件、帐篷 SKU 和拆分包裹均已完成，已加入最终完成列表。"
FORMAL_TENT_INSTRUCTION_REMARK_COMPLETION_PHRASE = "联系方式、文件夹、定制文件、帐篷 SKU、拆分包裹和说明书备注均已完成，已加入最终完成列表。"
FORMAL_TENT_WAREHOUSE_LOGISTICS_COMPLETION_PHRASE = "联系方式、文件夹、定制文件、帐篷 SKU、拆分包裹、说明书备注和仓库物流均已完成，已加入最终完成列表。"
SAFE_RETRY_COMPLETION_PHRASE = "文件夹和定制文件已完成校验；本次为安全/预览运行，不写入最终完成列表。"
SAFE_RETRY_TENT_SKU_COMPLETION_PHRASE = "联系方式、文件夹、定制文件和帐篷 SKU 均已完成校验；本次为安全/预览运行，不写入最终完成列表。"
SAFE_RETRY_TENT_PACKAGE_SPLIT_COMPLETION_PHRASE = "联系方式、文件夹、定制文件、帐篷 SKU 和拆分包裹均已完成校验；本次为安全/预览运行，不写入最终完成列表。"
SAFE_RETRY_TENT_INSTRUCTION_REMARK_COMPLETION_PHRASE = "联系方式、文件夹、定制文件、帐篷 SKU、拆分包裹和说明书备注均已完成校验；本次为安全/预览运行，不写入最终完成列表。"
SAFE_RETRY_TENT_WAREHOUSE_LOGISTICS_COMPLETION_PHRASE = "联系方式、文件夹、定制文件、帐篷 SKU、拆分包裹、说明书备注和仓库物流均已完成校验；本次为安全/预览运行，不写入最终完成列表。"


def adapt_completion_message_for_runtime(
    message: str,
    *,
    folder_write_enabled: bool,
    dedupe_write_enabled: bool,
) -> str:
    """根据运行开关调整成功文案，避免安全重测误报为正式完成。"""

    if folder_write_enabled and dedupe_write_enabled:
        return message
    return (
        message.replace(FORMAL_TENT_WAREHOUSE_LOGISTICS_COMPLETION_PHRASE, SAFE_RETRY_TENT_WAREHOUSE_LOGISTICS_COMPLETION_PHRASE)
        .replace(FORMAL_TENT_INSTRUCTION_REMARK_COMPLETION_PHRASE, SAFE_RETRY_TENT_INSTRUCTION_REMARK_COMPLETION_PHRASE)
        .replace(FORMAL_TENT_PACKAGE_SPLIT_COMPLETION_PHRASE, SAFE_RETRY_TENT_PACKAGE_SPLIT_COMPLETION_PHRASE)
        .replace(FORMAL_TENT_SKU_COMPLETION_PHRASE, SAFE_RETRY_TENT_SKU_COMPLETION_PHRASE)
        .replace(FORMAL_COMPLETION_PHRASE, SAFE_RETRY_COMPLETION_PHRASE)
        .replace(
            FORMAL_COMPLETION_SHORT_PHRASE,
            SAFE_RETRY_COMPLETION_PHRASE,
        )
    )


async def choose_contact_candidate_in_cmd(
    platform_order_no: str,
    system_order_no: str,
    contacts: list[ContactInfo],
) -> ContactInfo | None:
    """当存在多个或不完整联系方式候选时，让用户在 CMD 中确认。"""
    unique: list[ContactInfo] = []
    seen: set[tuple[str, str, str]] = set()
    for contact in contacts:
        key = contact_choice_identity(contact)
        if key is None or key in seen:
            continue
        seen.add(key)
        unique.append(contact)

    if not unique:
        return None
    if len(unique) == 1:
        contact = unique[0]
        missing_fields = missing_contact_fields(contact)
        if not missing_fields:
            return contact
        print("\n[联系方式不完整]")
        print(f"平台单号：{platform_order_no}")
        print(f"系统单号：{system_order_no}")
        print(f"已提取电话：{contact.phone or '-'}")
        print(f"已提取买家邮箱：{contact.email or '-'}")
        print(f"缺少字段：{'、'.join(missing_fields)}")
        answer = await asyncio.to_thread(input, "是否只写入已提取到的联系方式？输入 y 确认，输入其它内容跳过：")
        return contact if answer.strip().lower() in {"y", "yes", "1"} else None

    print("\n[选择联系方式候选]")
    print(f"平台单号：{platform_order_no}")
    print(f"系统单号：{system_order_no}")
    print("识别到多组电话/邮箱候选。请输入序号写回；直接回车则跳过该订单。")
    for index, contact in enumerate(unique, start=1):
        excerpt = (contact.source_excerpt or "").replace("\n", " ")[:120]
        missing_fields = missing_contact_fields(contact)
        missing_text = "完整" if not missing_fields else f"缺少：{'、'.join(missing_fields)}"
        print(f"{index}. 电话={contact.phone or '-'}；邮箱={contact.email or '-'}；{missing_text}；来源={excerpt}")
    answer = await asyncio.to_thread(input, "请选择序号：")
    try:
        selected_index = int(answer.strip())
    except ValueError:
        return None
    if not 1 <= selected_index <= len(unique):
        return None
    selected = unique[selected_index - 1]
    missing_fields = missing_contact_fields(selected)
    if not missing_fields:
        return selected
    answer = await asyncio.to_thread(
        input,
        f"所选联系方式缺少 {'、'.join(missing_fields)}。输入 y 只写入已提取字段：",
    )
    return selected if answer.strip().lower() in {"y", "yes", "1"} else None


def build_payment_source_for_window(paid_at_text: str | None, row_text: str | None) -> str:
    """构造付款时间窗口判断文本。

    付款时间解析器只认中文业务标签“付款时间/付款/支付时间”。
    """

    paid_at = (paid_at_text or "").strip()
    return f"付款时间 {paid_at}" if paid_at else (row_text or "")


def apply_amazon_order_total_if_missing(
    item: BatchOrderItem,
    summary: AmazonOrderSummaryResult,
) -> bool:
    """Fill only a genuinely absent Lingxing amount from Amazon ``OrderTotal``."""

    if (
        item.sales_revenue_status != "missing"
        or summary.status != AMAZON_ORDER_SUMMARY_RESOLVED
        or summary.order_total is None
    ):
        return False
    item.sales_revenue_total = (
        summary.order_total if summary.order_currency == "USD" else None
    )
    item.sales_revenue_currency = summary.order_currency
    item.sales_revenue_status = (
        "complete"
        if summary.order_currency == "USD"
        else "non_usd"
        if summary.order_currency
        else "currency_missing"
    )
    item.sales_revenue_source = "amazon_order_total"
    return True


async def _process_batch_order_item_impl(
    page,
    item: BatchOrderItem,
    amazon_quantity_client: AmazonOrderQuantityClient,
    dedupe_path: str | Path | None = None,
    payment_window_hours: float = DEFAULT_PAYMENT_WINDOW_HOURS,
    search_timeout_sec: int = 20,
    folder_root: str | Path | None = None,
    folder_date: str | None = None,
    create_folder: bool = True,
    download_custom_zip: bool = True,
    allow_sku_adjustment_page_write: bool | None = None,
    allow_package_split_page_write: bool | None = None,
    ignore_dedupe: bool = False,
    ignore_payment_window: bool = False,
    write_dedupe: bool = True,
    api_operations: CustomOrderApiOperations | None = None,
    interaction_policy: CustomOrderInteractionPolicy | None = None,
    log_dir: str | Path = "logs",
    validated_search_context: ValidatedOrderSearchContext | None = None,
    api_order_context: CustomOrderApiContext | None = None,
) -> dict[str, Any]:
    """处理单个批量订单候选项，串联联系方式、文件夹和 SKU 调整流程。"""
    item_started = time.monotonic()
    contact_choice_callback = (
        interaction_policy.choose_contact
        if interaction_policy is not None
        else choose_contact_candidate_in_cmd
    )
    writeback_confirm_callback = (
        interaction_policy.confirm_writeback
        if interaction_policy is not None
        else confirm_writeback_in_cmd
    )
    folder_confirm_callback = (
        interaction_policy.confirm_folder_creation
        if interaction_policy is not None
        else confirm_folder_creation_in_cmd
    )
    dedupe_read_enabled = bool(dedupe_path and not ignore_dedupe)
    if dedupe_read_enabled and is_platform_order_processed(dedupe_path, item.platform_order_no):
        return {
            "platform_order_no": item.platform_order_no,
            "system_order_no": item.system_order_no,
            "system_order_nos": [],
            "source_system_order_no": None,
            "asin": item.asin,
            "sku": item.sku,
            "parent_asin": item.parent_asin,
            "paid_at": item.paid_at_text,
            "status": "already_processed",
            "message": "平台单号已在最终完成列表中，进入详情前跳过。",
            "source_page": item.source_page,
            "source_scroll_top": item.source_scroll_top,
        }

    search_started = time.monotonic()
    if api_order_context is not None:
        if api_order_context.item.platform_order_no != item.platform_order_no:
            raise RuntimeError("API 订单上下文的平台单号与待处理订单不一致。")
        await close_order_detail_dialog(page)
        search_reused = False
        search_meta = await fill_order_search(
            page,
            item.platform_order_no,
            "platform",
        )
        system_order_nos = await wait_for_orders_in_list(
            page,
            item.platform_order_no,
            "platform",
            search_timeout_sec,
        )
        browser_search_count = 1
        search_meta = {
            **dict(search_meta),
            "browser_search_count": browser_search_count,
            "search_reused": False,
            "processing_search_ms": round(
                (time.monotonic() - search_started) * 1000
            ),
        }
    else:
        await close_order_detail_dialog(page)
        search_reused = False
        if (
            validated_search_context is not None
            and validated_search_context.order_no == item.platform_order_no
            and validated_search_context.search_kind == "platform"
        ):
            search_reused, search_meta, system_order_nos = (
                await _reuse_validated_order_search(page, validated_search_context)
            )
        else:
            search_meta = {}
            system_order_nos = []
        if not search_reused:
            search_meta = await fill_order_search(page, item.platform_order_no, "platform")
            system_order_nos = await wait_for_orders_in_list(
                page,
                item.platform_order_no,
                "platform",
                search_timeout_sec,
            )
        browser_search_count = (
            validated_search_context.browser_search_count + (0 if search_reused else 1)
            if validated_search_context is not None
            else 1
        )
        search_meta = {
            **dict(search_meta),
            "browser_search_count": browser_search_count,
            "search_reused": search_reused,
            "processing_search_ms": round((time.monotonic() - search_started) * 1000),
        }
    if not search_meta.get("search_validation_ok"):
        return {
            "platform_order_no": item.platform_order_no,
            "system_order_no": item.system_order_no,
            "system_order_nos": system_order_nos,
            "source_system_order_no": None,
            "asin": item.asin,
            "sku": item.sku,
            "parent_asin": item.parent_asin,
            "paid_at": item.paid_at_text,
            "status": "search_failed",
            "message": str(search_meta.get("search_validation_message") or "平台单号搜索框校验失败。"),
            "search_meta": search_meta,
            "browser_search_count": browser_search_count,
            "source_page": item.source_page,
            "source_scroll_top": item.source_scroll_top,
        }
    if not system_order_nos:
        return {
            "platform_order_no": item.platform_order_no,
            "system_order_no": item.system_order_no,
            "system_order_nos": [],
            "source_system_order_no": None,
            "asin": item.asin,
            "sku": item.sku,
            "parent_asin": item.parent_asin,
            "paid_at": item.paid_at_text,
            "status": "search_no_results",
            "message": f"搜索平台单号 {item.platform_order_no} 后未找到订单。",
            "search_meta": search_meta,
            "browser_search_count": browser_search_count,
            "source_page": item.source_page,
            "source_scroll_top": item.source_scroll_top,
        }

    list_asin_text = item.asin or ""
    detected_asins = extract_asins([list_asin_text, item.row_text])
    product_match = match_supported_product(list_asin_text) or match_supported_product(item.row_text)
    payment_source = build_payment_source_for_window(item.paid_at_text, item.row_text)
    payment_status = classify_recent_payment_window(payment_source, hours=payment_window_hours)
    paid_at_text = latest_payment_text(payment_source) or item.paid_at_text
    payload: dict[str, Any] = {
        "platform_order_no": item.platform_order_no,
        "system_order_no": item.system_order_no,
        "system_order_nos": system_order_nos,
        "source_system_order_no": None,
        "detected_asins": detected_asins,
        "asin": product_match.asin if product_match else None,
        "sku": item.sku,
        "logistics": item.logistics,
        "sales_revenue_total": item.sales_revenue_total,
        "sales_revenue_currency": item.sales_revenue_currency,
        "sales_revenue_status": item.sales_revenue_status,
        "sales_revenue_source": item.sales_revenue_source,
        "parent_asin": product_match.parent_asin if product_match else None,
        "product_type": product_match.product_type if product_match else item.product_type,
        "contact_prompt_order": list(product_match.contact_prompts) if product_match else [],
        "payment_window_hours": payment_window_hours,
        "payment_status": payment_status,
        "paid_at": paid_at_text,
        "phone": None,
        "email": None,
        "status": "failed",
        "message": "",
        "source_excerpt": "",
        "search_meta": search_meta,
        "browser_search_count": browser_search_count,
        "timings": {
            "search_ms": search_meta["processing_search_ms"],
            "initial_search_ms": (
                validated_search_context.search_duration_ms
                if validated_search_context is not None
                else search_meta["processing_search_ms"]
            ),
        },
        "source_page": item.source_page,
        "source_scroll_top": item.source_scroll_top,
        "dedupe_read_enabled": dedupe_read_enabled,
        "dedupe_write_enabled": write_dedupe,
    }
    unique_system_order_nos = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in system_order_nos
            if str(value or "").strip()
        )
    )
    payload["system_order_nos"] = unique_system_order_nos
    if item.system_order_no and item.system_order_no not in unique_system_order_nos:
        payload["status"] = "search_context_mismatch"
        payload["message"] = (
            f"平台单号搜索结果不包含列表中的系统单号 {item.system_order_no}；"
            f"实际结果：{unique_system_order_nos}。为避免写错订单已停止。"
        )
        if api_order_context is None:
            await close_order_detail_dialog(page)
        return payload
    if len(unique_system_order_nos) != 1:
        workflow_record = (
            load_order_workflow_record(dedupe_path, item.platform_order_no)
            if dedupe_read_enabled and dedupe_path
            else None
        )
        instruction_ready = bool(
            workflow_record
            and (
                not normalize_bool(workflow_record.get("instruction_remark_required"))
                or normalize_bool(workflow_record.get("instruction_remark_complete"))
            )
        )
        high_value_resume = bool(
            workflow_record
            and (
                workflow_record.get("sku_adjustment_workflow_kind") == HIGH_VALUE_WORKFLOW_KIND
                or workflow_record.get("product_type") in NON_TENT_HIGH_VALUE_PRODUCT_TYPES
            )
            and normalize_bool(workflow_record.get("package_split_complete"))
        )
        if high_value_resume and not normalize_bool(
            workflow_record.get("instruction_remark_complete")
        ):
            instruction_payload = await run_persisted_high_value_instruction_remark_stage(
                page,
                item,
                workflow_record=workflow_record,
                candidate_system_order_nos=unique_system_order_nos,
                dedupe_path=dedupe_path,
                write_dedupe=write_dedupe and create_folder,
                allow_page_write=(
                    create_folder
                    if allow_package_split_page_write is None
                    else bool(allow_package_split_page_write)
                ),
                read_dedupe=dedupe_read_enabled,
                api_operations=api_operations,
                interaction_policy=interaction_policy,
            )
            payload.update(instruction_payload)
            payload["system_order_nos"] = unique_system_order_nos
            if payload.get("instruction_remark_complete"):
                payload["status"] = "updated"
                payload["message"] = (
                    "已从持久化换货时间恢复说明书备注并写入 Instruction 系统订单；"
                    "仓库物流不在本流程范围内。"
                )
            else:
                payload["status"] = "updated_folder_created_instruction_remark_failed"
                payload["message"] = (
                    "说明书备注处理失败："
                    + str(
                        payload.get("instruction_remark_error")
                        or payload.get("instruction_remark_status")
                        or "无法恢复说明书备注。"
                    )
                )
            return payload
        if high_value_resume and instruction_ready and not normalize_bool(
            workflow_record.get("warehouse_logistics_complete")
        ):
            payload["warehouse_logistics_recorded"] = record_warehouse_logistics_if_allowed(
                dedupe_path,
                item.platform_order_no,
                str(workflow_record.get("system_order_no") or item.system_order_no or ""),
                write_enabled=write_dedupe and create_folder,
                warehouse_status="not_required_non_tent",
                decisions=[],
                write_results=[],
                result_detail="非帐篷订单不处理仓库物流；金额达到 200 USD/CAD 时仅执行高金额换货拆单流程。",
                warehouse_required=False,
            )
            payload["warehouse_logistics_required"] = False
            payload["warehouse_logistics_complete"] = True
            payload["warehouse_logistics_status"] = "not_required_non_tent"
            payload["status"] = "updated"
            payload["message"] = "说明书备注已完成；仓库物流不在本流程范围内。"
            payload["system_order_nos"] = unique_system_order_nos
            return payload
        warehouse_resume_ready = bool(
            workflow_record
            and workflow_record.get("product_type") == PRODUCT_TYPE_TENT
            and normalize_bool(workflow_record.get("package_split_complete"))
            and instruction_ready
            and normalize_bool(workflow_record.get("warehouse_logistics_required"))
            and not normalize_bool(workflow_record.get("warehouse_logistics_complete"))
        )
        if warehouse_resume_ready:
            try:
                restored_plan = tent_sku_plan_from_routing_input(
                    workflow_record.get("warehouse_logistics_plan_input")
                )
            except Exception as exc:
                payload["status"] = "updated_folder_created_warehouse_logistics_failed"
                payload["warehouse_logistics_status"] = "plan_input_invalid"
                payload["warehouse_logistics_error"] = str(exc)
                payload["message"] = (
                    f"仓库物流处理失败：拆单后的计划无法恢复：{exc}"
                )
                payload["system_order_nos"] = unique_system_order_nos
                return payload
            if not normalize_us_postal_code(restored_plan.destination.postal_code):
                original_system_order_no = str(
                    workflow_record.get("system_order_no")
                    or item.system_order_no
                    or ""
                ).strip()
                try:
                    await close_order_detail_dialog(page)
                    await click_system_order(page, original_system_order_no)
                    await wait_for_detail(page, original_system_order_no)
                    await assert_current_detail_order(
                        page,
                        original_system_order_no,
                        item.platform_order_no,
                        "warehouse postal refresh",
                    )
                    (
                        refreshed_destination,
                        refreshed_shipping_text,
                    ) = await _read_detail_destination_with_web_region(
                        page,
                        original_system_order_no,
                    )
                    refreshed_postal_code = refreshed_destination.postal_code
                    refreshed_postal_source = refreshed_destination.postal_source
                    refreshed_api_error = refreshed_destination.api_error
                    refreshed_request_id = refreshed_destination.request_id
                    destination_region = parse_destination_region(
                        refreshed_shipping_text
                    )
                    destination_region.postal_code = normalize_us_postal_code(
                        refreshed_postal_code
                    )
                    destination_region.postal_source = refreshed_postal_source
                    destination_region.postal_error = refreshed_api_error
                    restored_plan.destination = destination_region
                    payload["shipping_address_text"] = _short_text(
                        refreshed_shipping_text,
                        1000,
                    )
                    payload["shipping_postal_code"] = destination_region.postal_code
                    payload["shipping_postal_source"] = destination_region.postal_source
                    payload["shipping_postal_api_diagnostic"] = destination_region.postal_error
                    payload["shipping_postal_request_id"] = refreshed_request_id
                except Exception as exc:
                    payload["status"] = (
                        "updated_folder_created_warehouse_logistics_failed"
                    )
                    payload["warehouse_logistics_status"] = (
                        "warehouse_logistics_manual_review"
                    )
                    payload["warehouse_logistics_error"] = (
                        "仓库阶段无法从原始系统单重新读取邮编："
                        f"{str(exc) or type(exc).__name__}"
                    )
                    payload["message"] = (
                        "仓库物流处理失败："
                        + str(payload["warehouse_logistics_error"])
                    )
                    payload["system_order_nos"] = unique_system_order_nos
                    if api_order_context is None:
                        await close_order_detail_dialog(page)
                    return payload
                if api_order_context is None:
                    await close_order_detail_dialog(page)
                if not restored_plan.destination.postal_code:
                    payload["status"] = (
                        "updated_folder_created_warehouse_logistics_failed"
                    )
                    payload["warehouse_logistics_status"] = (
                        "warehouse_logistics_manual_review"
                    )
                    payload["warehouse_logistics_error"] = (
                        restored_plan.destination.postal_error
                        or "领星订单 API 未取得有效五位邮编，禁止自动设置仓库物流。"
                    )
                    payload["message"] = (
                        "仓库物流处理失败："
                        + str(payload["warehouse_logistics_error"])
                    )
                    payload["system_order_nos"] = unique_system_order_nos
                    return payload
                if write_dedupe and dedupe_path:
                    try:
                        update_warehouse_logistics_plan_input(
                            dedupe_path,
                            item.platform_order_no,
                            tent_sku_plan_to_routing_input(restored_plan),
                        )
                        payload["warehouse_logistics_plan_refreshed"] = True
                    except Exception as exc:
                        payload["status"] = (
                            "updated_folder_created_warehouse_logistics_failed"
                        )
                        payload["warehouse_logistics_status"] = (
                            "warehouse_logistics_manual_review"
                        )
                        payload["warehouse_logistics_error"] = (
                            "邮编已重新读取，但仓库物流恢复计划持久化失败："
                            f"{str(exc) or type(exc).__name__}"
                        )
                        payload["message"] = (
                            "仓库物流处理失败："
                            + str(payload["warehouse_logistics_error"])
                        )
                        payload["system_order_nos"] = unique_system_order_nos
                        return payload
            warehouse_payload = await run_tent_warehouse_logistics_stage(
                page,
                item,
                str(workflow_record.get("system_order_no") or item.system_order_no or ""),
                None,
                None,
                package_split_system_order_nos=list(
                    workflow_record.get("package_split_system_order_nos")
                    or unique_system_order_nos
                ),
                dedupe_path=dedupe_path,
                write_dedupe=write_dedupe and create_folder,
                allow_page_write=(
                    create_folder
                    if allow_package_split_page_write is None
                    else bool(allow_package_split_page_write)
                ),
                read_dedupe=dedupe_read_enabled,
                api_operations=api_operations,
                interaction_policy=interaction_policy,
                sku_plan_override=restored_plan,
                runtime_system_order_no=str(
                    workflow_record.get("system_order_no")
                    or item.system_order_no
                    or ""
                ),
            )
            payload.update(warehouse_payload)
            payload["sku_adjustment_required"] = True
            payload["source_system_order_no"] = workflow_record.get("system_order_no")
            payload["system_order_nos"] = unique_system_order_nos
            if payload.get("warehouse_logistics_complete"):
                payload["status"] = "updated"
                payload["message"] = FORMAL_TENT_WAREHOUSE_LOGISTICS_COMPLETION_PHRASE
            else:
                payload["status"] = "updated_folder_created_warehouse_logistics_failed"
                payload["message"] = (
                    "仓库物流处理失败："
                    + str(
                        payload.get("warehouse_logistics_error")
                        or payload.get("warehouse_logistics_status")
                        or "-"
                    )
                )
            return payload
        payload["status"] = "split_order_after_search"
        payload["message"] = f"平台单号 {item.platform_order_no} 匹配到 {len(unique_system_order_nos)} 个系统单号，按拆分订单跳过。"
        payload["system_order_nos"] = unique_system_order_nos
        if api_order_context is None:
            await close_order_detail_dialog(page)
        return payload
    if not product_match:
        payload["status"] = "not_tent"
        payload["message"] = "订单 ASIN/SKU 不在当前支持的定制品类中，已跳过。"
        if api_order_context is None:
            await close_order_detail_dialog(page)
        return payload

    item.asin = product_match.asin
    item.parent_asin = product_match.parent_asin
    item.product_type = product_match.product_type
    item.paid_at_text = paid_at_text

    if payment_status != "recent" and not ignore_payment_window:
        payload["status"] = "payment_time_unknown" if payment_status == "unknown" else "payment_window_expired"
        payload["message"] = (
            "未能从订单列表识别付款时间，已跳过。"
            if payment_status == "unknown"
            else f"付款时间不在最近 {payment_window_hours:g} 小时内，已跳过。"
        )
        if api_order_context is None:
            await close_order_detail_dialog(page)
        return payload

    system_order_no = unique_system_order_nos[0]
    detail_started = time.monotonic()
    await close_order_detail_dialog(page)
    await click_system_order(page, system_order_no)
    await wait_for_detail(page, system_order_no)
    await assert_current_detail_order(
        page,
        system_order_no,
        item.platform_order_no,
        "before extraction",
    )
    (
        shipping_destination,
        shipping_address_text,
    ) = await _read_detail_destination_with_web_region(page, system_order_no)
    shipping_postal_code = shipping_destination.postal_code
    shipping_postal_source = shipping_destination.postal_source
    shipping_postal_error = shipping_destination.api_error
    shipping_request_id = shipping_destination.request_id
    payload["shipping_address_text"] = _short_text(shipping_address_text, 1000)
    payload["shipping_postal_code"] = shipping_postal_code
    payload["shipping_postal_source"] = shipping_postal_source
    payload["shipping_postal_api_diagnostic"] = shipping_postal_error
    payload["shipping_postal_request_id"] = shipping_request_id
    payload["timings"]["detail_open_and_destination_ms"] = round(
        (time.monotonic() - detail_started) * 1000
    )
    folder_context_started = time.monotonic()
    folder_context = await collect_order_folder_json_context(
        page,
        item,
        amazon_quantity_client,
        system_order_no,
        staging_root=Path(log_dir) / "custom_zip_staging",
        download_custom_zip=download_custom_zip,
        api_operations=api_operations,
        interaction_policy=interaction_policy,
    )
    payload["timings"]["folder_context_ms"] = round(
        (time.monotonic() - folder_context_started) * 1000
    )
    payload["timings"].update(
        {
            f"folder_context_{key}": int(value or 0)
            for key, value in dict(
                folder_context.get("context_timings") or {}
            ).items()
        }
    )
    await assert_current_detail_order(
        page,
        system_order_no,
        item.platform_order_no,
        "before writeback",
    )
    quantity_result = folder_context.get("amazon_quantity_result")
    if isinstance(quantity_result, AmazonOrderQuantityResult):
        payload.update(quantity_result.to_log_dict())
    order_summary = folder_context.get("amazon_order_summary_result")
    if isinstance(order_summary, AmazonOrderSummaryResult):
        payload.update(order_summary.to_log_dict())
        if apply_amazon_order_total_if_missing(item, order_summary):
            payload["sales_revenue_total"] = item.sales_revenue_total
            payload["sales_revenue_currency"] = item.sales_revenue_currency
            payload["sales_revenue_status"] = item.sales_revenue_status
            payload["sales_revenue_source"] = item.sales_revenue_source
        if (
            not shipping_address_text
            and order_summary.status == AMAZON_ORDER_SUMMARY_RESOLVED
            and order_summary.shipping_address_text
        ):
            shipping_address_text = order_summary.shipping_address_text
            shipping_postal_code = normalize_us_postal_code(
                order_summary.postal_code
            )
            shipping_postal_source = "amazon_orders_api"
            shipping_postal_error = None
            payload["shipping_address_text"] = _short_text(
                shipping_address_text,
                1000,
            )
            payload["shipping_postal_code"] = shipping_postal_code
            payload["shipping_postal_source"] = shipping_postal_source
            payload["shipping_postal_api_diagnostic"] = None
    zip_bundle = folder_context.get("zip_bundle")
    if zip_bundle is not None:
        payload.update(zip_bundle.to_log_dict())
    payload["recipient_name"] = folder_context.get("recipient_name")
    payload["recipient_name_source"] = folder_context.get("recipient_name_source")
    payload["order_line_warnings"] = folder_context.get("order_line_warnings") or []
    payload["order_line_error"] = folder_context.get("order_line_error")
    payload["order_folder_lines"] = [
        {
            "asin": line.asin,
            "sku": line.sku,
            "parent_asin": line.parent_asin,
            "product_type": line.product_type,
            "quantity": line.quantity,
            "order_item_id": line.order_item_id,
        }
        for line in (folder_context.get("order_lines") or [])
    ]
    order_lines_for_sku = folder_context.get("order_lines") or []
    sku_adjustment_required = order_requires_tent_sku_adjustment(
        item,
        order_lines_for_sku,
        shipping_address_text=shipping_address_text,
    )
    folder_already_complete = bool(
        dedupe_read_enabled
        and sku_adjustment_required
        and is_folder_complete(dedupe_path, item.platform_order_no)
    )
    sku_adjustment_already_done = bool(
        dedupe_read_enabled
        and sku_adjustment_required
        and is_sku_adjustment_done(dedupe_path, item.platform_order_no)
    )
    package_split_already_done = bool(
        dedupe_read_enabled
        and sku_adjustment_required
        and is_package_split_done(dedupe_path, item.platform_order_no)
    )
    instruction_remark_already_done = bool(
        dedupe_read_enabled
        and sku_adjustment_required
        and is_instruction_remark_done(dedupe_path, item.platform_order_no)
    )
    warehouse_logistics_already_done = bool(
        dedupe_read_enabled
        and sku_adjustment_required
        and is_warehouse_logistics_done(dedupe_path, item.platform_order_no)
    )
    payload["sku_adjustment_required"] = sku_adjustment_required
    payload["folder_already_complete"] = folder_already_complete
    payload["sku_adjustment_already_done"] = sku_adjustment_already_done
    payload["package_split_already_done"] = package_split_already_done
    payload["instruction_remark_already_done"] = instruction_remark_already_done
    payload["warehouse_logistics_already_done"] = warehouse_logistics_already_done
    sku_adjustment_page_write_enabled = (
        create_folder if allow_sku_adjustment_page_write is None else bool(allow_sku_adjustment_page_write)
    )
    package_split_page_write_enabled = (
        create_folder if allow_package_split_page_write is None else bool(allow_package_split_page_write)
    )
    payload["sku_adjustment_page_write_enabled"] = sku_adjustment_page_write_enabled
    payload["package_split_page_write_enabled"] = package_split_page_write_enabled
    payload["instruction_remark_page_write_enabled"] = package_split_page_write_enabled
    payload["warehouse_logistics_page_write_enabled"] = package_split_page_write_enabled
    if (zip_bundle is None or getattr(zip_bundle, "status", "ok") != "ok") or (
        isinstance(quantity_result, AmazonOrderQuantityResult) and quantity_result.status != AMAZON_QUANTITY_RESOLVED
    ) or folder_context.get("order_line_error"):
        folder_result = json_context_failure_folder_result(folder_context, folder_root=folder_root or DEFAULT_FOLDER_ROOT)
        payload.update(folder_result.to_log_dict())
        payload["status"] = "updated_folder_failed"
        payload["message"] = (
            "定制文件准备失败："
            f"{format_folder_failure_reason(folder_result)}"
        )
        if api_order_context is None:
            await close_order_detail_dialog(page)
        return payload

    contact_started = time.monotonic()
    contact_candidates = extract_contact_candidates_from_json_items(getattr(zip_bundle, "customization_items", []) if zip_bundle is not None else [])
    contact_writeback_already_done = bool(
        dedupe_read_enabled and is_contact_writeback_done(dedupe_path, item.platform_order_no)
    )
    texts: list[str] = []
    payload["extracted_contacts"] = [
        {
            "system_order_no": system_order_no,
            "phone": contact.phone,
            "email": contact.email,
            "missing_fields": missing_contact_fields(contact),
            "source_excerpt": contact.source_excerpt,
        }
        for contact in contact_candidates
    ]
    payload["candidate_text_count"] = len(getattr(zip_bundle, "customization_items", []) if zip_bundle is not None else [])
    payload["fixed_prompt_text_count"] = len(contact_candidates)
    payload["customer_email_provided"] = any(
        bool(contact.email) for contact in contact_candidates
    )
    payload["customer_phone_provided"] = any(
        bool(contact.phone) for contact in contact_candidates
    )
    payload["contact_value_source"] = "customization_json"
    payload["contact_writeback_already_done"] = contact_writeback_already_done

    skip_contact_writeback = False
    contact_stage_status = "written"
    if contact_writeback_already_done and not contact_candidates:
        selected_contact = ContactInfo(phone=None, email=None, source_count=0, source_excerpt="zip JSON missing contact")
        skip_contact_writeback = True
        contact_stage_status = "already_done"
        payload["contact_writeback_skipped"] = True
        payload["contact_writeback_skip_reason"] = "contact_writeback_already_done"
        notify_contact_writeback_already_done_in_cmd(item.platform_order_no, system_order_no)
    elif not contact_candidates:
        selected_contact = ContactInfo(phone=None, email=None, source_count=0, source_excerpt="zip JSON missing contact")
        skip_contact_writeback = True
        contact_stage_status = "skipped_no_contact"
        payload["contact_writeback_skipped"] = True
        payload["contact_writeback_skip_reason"] = "customization_json_missing_contact"
        notify_no_contact_writeback_in_cmd(item.platform_order_no, system_order_no)
    else:
        selected_contact = await contact_choice_callback(
            item.platform_order_no,
            system_order_no,
            contact_candidates,
        )

    if selected_contact is None:
        payload["status"] = "contact_choice_skipped" if contact_candidates else "missing_contact"
        if not contact_candidates:
            payload["detail_text_preview"] = build_detail_text_preview(texts)
            payload["message"] = (
                "联系方式解析失败：定制化 JSON 中未解析到电话或邮箱。"
            )
        else:
            payload["message"] = "联系方式处理取消：用户取消写回。"
        if api_order_context is None:
            await close_order_detail_dialog(page)
        return payload

    payload["phone"] = selected_contact.phone
    payload["email"] = selected_contact.email
    payload["customer_email_provided"] = bool(selected_contact.email)
    payload["customer_phone_provided"] = bool(selected_contact.phone)
    payload["recipient_name"] = str(folder_context.get("recipient_name") or "").strip()
    payload["writeback_fields"] = contact_writeback_fields(selected_contact)
    payload["missing_contact_fields"] = missing_contact_fields(selected_contact)
    payload["source_excerpt"] = selected_contact.source_excerpt
    notification_contact_capture = (
        interaction_policy.capture_notification_contact
        if interaction_policy is not None
        else None
    )
    if notification_contact_capture is not None:
        try:
            payload["shipment_notification_contact_persisted"] = bool(
                await notification_contact_capture(
                    item.platform_order_no,
                    system_order_no,
                    payload["recipient_name"],
                    selected_contact,
                )
            )
        except Exception as exc:
            payload["shipment_notification_contact_persisted"] = False
            payload["shipment_notification_contact_persist_error"] = type(exc).__name__
    contact_guard_blocked = False
    contact_verification_method: str | None = None
    if skip_contact_writeback:
        saved = True
        message = (
            "联系方式此前已完成，本轮跳过写回。"
            if contact_writeback_already_done
            else "定制化 JSON 中没有电话/邮箱，本次不写回联系方式。"
        )
        writeback_result = ContactWritebackResult(
            status=contact_stage_status,
            completed=True,
            mutated=False,
            message=message,
            before_values={},
        )
    else:
        try:
            await assert_current_detail_order(
                page,
                system_order_no,
                item.platform_order_no,
                "before contact writeback",
            )
        except Exception:
            # Never recover a lost or changed detail by searching again here.
            # The caller must fail this order instead of risking a write to a
            # different order that happened to become visible in the table.
            await close_order_detail_dialog(page)
            raise
        payload["contact_browser_search_count"] = 0
        payload["contact_browser_detail_reused"] = True
        try:
            current_contact_values = await read_shipping_contact_values(page)
            contact_to_write = _contact_write_delta(
                selected_contact,
                current_contact_values,
            )
        except Exception as exc:
            current_contact_values = {}
            contact_to_write = selected_contact
            payload["contact_precheck_error"] = type(exc).__name__

        async def confirm_browser_contact(context: dict[str, Any]) -> bool:
            nonlocal contact_guard_blocked
            if not await writeback_confirm_callback(context):
                return False
            allowed = await runtime_write_allowed(
                interaction_policy,
                "contact_browser",
                item.platform_order_no,
                system_order_no,
            )
            contact_guard_blocked = not allowed
            return allowed

        if not contact_to_write.phone and not contact_to_write.email:
            saved = True
            message = "重新读取网页后确认联系方式与定制 JSON 一致，无需编辑或保存。"
            contact_stage_status = "already_current"
            contact_verification_method = (
                "browser_current_detail_identity_and_values"
            )
            payload["contact_writeback_skipped"] = True
            payload["contact_writeback_skip_reason"] = "already_current"
            payload["contact_before_values"] = dict(current_contact_values)
            writeback_result = ContactWritebackResult(
                status=contact_stage_status,
                completed=True,
                mutated=False,
                message=message,
                before_values=dict(current_contact_values),
            )
        else:
            saved, message = await update_current_detail_contact(
                page,
                contact_to_write,
                expected_system_order_no=system_order_no,
                expected_platform_order_no=item.platform_order_no,
                source_system_order_no=system_order_no,
                confirm_callback=confirm_browser_contact,
            )
            if saved:
                contact_verification_method = "browser_detail_reopen"
            writeback_result = ContactWritebackResult(
                status="written" if saved else "failed",
                completed=saved,
                mutated=saved,
                message=message,
                before_values=dict(current_contact_values),
            )
        payload["contact_writeback_source"] = "browser"
        payload["contact_write_status"] = writeback_result.status
        payload["contact_write_mutated"] = writeback_result.mutated
        payload["contact_written_fields"] = contact_writeback_fields(contact_to_write)
        if contact_to_write.phone:
            payload["phone_writeback_source"] = "browser"
        if contact_to_write.email:
            payload["buyer_email_writeback_source"] = "browser"
        if contact_guard_blocked:
            message = mark_runtime_write_blocked(
                payload,
                stage="contact_browser",
                stage_label="联系方式网页写回",
                status_key="contact_status",
                error_key="contact_error",
            )
    # Contact handling never leaves the order detail open, including no-op,
    # missing-contact, rejected-confirmation and failed-save paths.  Later
    # workflow stages open the exact order they need independently.
    await close_order_detail_dialog(page)
    payload.setdefault("contact_write_status", writeback_result.status)
    payload.setdefault("contact_write_mutated", writeback_result.mutated)
    payload["contact_writeback_verified"] = bool(
        saved and contact_candidates and not skip_contact_writeback
    )
    if payload["contact_writeback_verified"]:
        payload["contact_verification_method"] = contact_verification_method
    payload["timings"]["contact_ms"] = round(
        (time.monotonic() - contact_started) * 1000
    )
    payload["update_messages"] = [f"{system_order_no}: {message}"]
    if saved:
        payload["source_system_order_no"] = system_order_no
        payload["updated_system_order_nos"] = [system_order_no]
        contact_recorded = False
        if contact_candidates and not skip_contact_writeback:
            contact_recorded = record_contact_writeback_if_allowed(
                dedupe_path,
                item.platform_order_no,
                system_order_no,
                contact_status=contact_stage_status,
                contact_verified=True,
                contact_verification_method=contact_verification_method,
                write_enabled=write_dedupe,
            )
        if dedupe_path and not write_dedupe:
            payload["dedupe_write_skipped"] = True
        payload["contact_writeback_recorded"] = contact_recorded or contact_writeback_already_done
        order_lines = folder_context.get("order_lines") or []
        if not order_lines:
            folder_result = json_context_failure_folder_result(folder_context, folder_root=folder_root or DEFAULT_FOLDER_ROOT)
        else:
            folder_result = build_and_create_order_folder_from_lines(
                order_item=item,
                order_lines=order_lines,
                recipient_name=str(folder_context.get("recipient_name") or ""),
                payment_time=item.paid_at_text,
                folder_root=folder_root or DEFAULT_FOLDER_ROOT,
                override_date=folder_date,
                create_folder=False,
                logistics=item.logistics,
                shipping_address_text=shipping_address_text,
            )
        if folder_result.status == FOLDER_EXISTING_PLATFORM_ORDER:
            notify_existing_folder_in_cmd(
                item.platform_order_no,
                system_order_no,
                folder_result,
                folder_write_enabled=create_folder,
                dedupe_write_enabled=write_dedupe,
            )
        elif create_folder and not folder_already_complete and folder_result.status in SUCCESS_FOLDER_STATUSES:
            if await folder_confirm_callback(item.platform_order_no, system_order_no, folder_result):
                if not await runtime_write_allowed(
                    interaction_policy,
                    "folder_create",
                    item.platform_order_no,
                    system_order_no,
                ):
                    payload.update(folder_result.to_log_dict())
                    message = mark_runtime_write_blocked(
                        payload,
                        stage="folder_create",
                        stage_label="订单文件夹创建",
                        status_key="folder_status",
                        error_key="folder_error",
                    )
                    payload["status"] = "folder_write_blocked"
                    payload["message"] = message
                    await close_order_detail_dialog(page)
                    return payload
                folder_result = create_order_folder_from_preview(folder_result, create_folder=True, platform_order_no=item.platform_order_no)
            else:
                folder_result = cancel_folder_creation_result(folder_result)
        payload.update(folder_result.to_log_dict())
        if folder_result.status in SUCCESS_FOLDER_STATUSES:
            if folder_already_complete:
                # 文件夹阶段已完成的帐篷订单下轮只补 SKU，不能重复复制 zip 或再次写完整文件夹名。
                finalize_result = {
                    "custom_zip_status": "custom_zip_skipped_folder_already_complete",
                    "custom_zip_copied_files": [],
                    "custom_zip_error": None,
                    "custom_zip_staging_cleanup_status": None,
                    "custom_zip_staging_cleanup_error": None,
                    "full_folder_name_txt": None,
                }
            else:
                if create_folder and not await runtime_write_allowed(
                    interaction_policy,
                    "folder_finalize",
                    item.platform_order_no,
                    system_order_no,
                ):
                    message = mark_runtime_write_blocked(
                        payload,
                        stage="folder_finalize",
                        stage_label="订单文件夹定制文件写入",
                        status_key="folder_status",
                        error_key="folder_error",
                    )
                    payload["status"] = "folder_write_blocked"
                    payload["message"] = message
                    await close_order_detail_dialog(page)
                    return payload
                finalize_result = finalize_custom_zip_files_for_folder(
                    folder_result,
                    folder_context,
                    allow_folder_write=create_folder,
                )
            payload.update(finalize_result)
            zip_ok = (
                finalize_result.get("custom_zip_status") in {CUSTOM_ZIP_MOVED, CUSTOM_ZIP_DISABLED}
                or folder_already_complete
                or not create_folder
            )
            if zip_ok:
                folder_recorded = folder_already_complete or record_folder_complete_if_allowed(
                    dedupe_path,
                    item.platform_order_no,
                    system_order_no,
                    write_enabled=write_dedupe and create_folder,
                    product_type=item.product_type,
                    sku_adjustment_required=sku_adjustment_required,
                )
                payload["folder_complete_recorded"] = folder_recorded
                if sku_adjustment_required:
                    sku_payload = await run_tent_sku_adjustment_stage(
                        page,
                        item,
                        system_order_no,
                        folder_result,
                        order_lines,
                        shipping_address_text=shipping_address_text,
                        dedupe_path=dedupe_path,
                        write_dedupe=write_dedupe and create_folder,
                        allow_page_write=sku_adjustment_page_write_enabled,
                        read_dedupe=dedupe_read_enabled,
                        api_operations=api_operations,
                        interaction_policy=interaction_policy,
                    )
                    payload.update(sku_payload)
                    sku_stage_complete = bool(payload.get("sku_adjustment_complete"))
                    if _sku_stage_allows_package_split(
                        payload,
                        package_split_page_write_enabled=package_split_page_write_enabled,
                    ):
                        package_payload = await run_tent_package_split_stage(
                            page,
                            item,
                            system_order_no,
                            folder_result,
                            order_lines,
                            shipping_address_text=shipping_address_text,
                            dedupe_path=dedupe_path,
                            write_dedupe=write_dedupe and create_folder,
                            allow_page_write=package_split_page_write_enabled,
                            shipping_postal_source=shipping_postal_source,
                            shipping_postal_error=shipping_postal_error,
                            read_dedupe=dedupe_read_enabled,
                            api_operations=api_operations,
                            interaction_policy=interaction_policy,
                        )
                        payload.update(package_payload)
                        if payload.get("package_split_complete"):
                            instruction_payload = await run_tent_instruction_remark_stage(
                                page,
                                item,
                                system_order_no,
                                folder_result,
                                order_lines,
                                shipping_address_text=shipping_address_text,
                                package_split_system_order_nos=payload.get("package_split_system_order_nos") or [],
                                package_split_instruction_system_order_no=payload.get(
                                    "package_split_instruction_system_order_no"
                                ),
                                instruction_remark_confirmation_granted=payload.get(
                                    "instruction_remark_confirmation_granted"
                                ),
                                dedupe_path=dedupe_path,
                                write_dedupe=write_dedupe and create_folder,
                                allow_page_write=package_split_page_write_enabled,
                                shipping_postal_source=shipping_postal_source,
                                shipping_postal_error=shipping_postal_error,
                                read_dedupe=dedupe_read_enabled,
                                api_operations=api_operations,
                                interaction_policy=interaction_policy,
                            )
                            payload.update(instruction_payload)
                            if payload.get("instruction_remark_complete") and _is_high_value_workflow(
                                item,
                                order_lines,
                            ):
                                payload["status"] = "updated"
                                payload["message"] = (
                                    "联系方式、文件夹、定制文件、原商品行换 Instruction、"
                                    "原 SKU 聚合回加、拆单和说明书备注均已完成；仓库物流不在本流程范围内。"
                                )
                            elif payload.get("instruction_remark_complete"):
                                warehouse_payload = await run_tent_warehouse_logistics_stage(
                                    page,
                                    item,
                                    system_order_no,
                                    folder_result,
                                    order_lines,
                                    shipping_address_text=shipping_address_text,
                                    package_split_system_order_nos=payload.get(
                                        "package_split_system_order_nos"
                                    )
                                    or [],
                                    dedupe_path=dedupe_path,
                                    write_dedupe=write_dedupe and create_folder,
                                    allow_page_write=package_split_page_write_enabled,
                                    shipping_postal_source=shipping_postal_source,
                                    shipping_postal_error=shipping_postal_error,
                                    read_dedupe=dedupe_read_enabled,
                                    api_operations=api_operations,
                                    interaction_policy=interaction_policy,
                                    package_split_projected_packages=payload.get(
                                        "package_split_projected_packages"
                                    ),
                                )
                                payload.update(warehouse_payload)
                                if payload.get("warehouse_logistics_complete"):
                                    payload["status"] = "updated"
                                    payload["message"] = (
                                        FORMAL_TENT_WAREHOUSE_LOGISTICS_COMPLETION_PHRASE
                                        if sku_stage_complete
                                        else "联系方式、文件夹、定制文件、帐篷 SKU 计划、拆分包裹、说明书备注和仓库物流均已完成；SKU 页面写入未执行。"
                                    )
                                else:
                                    payload["status"] = (
                                        "updated_folder_created_warehouse_logistics_failed"
                                    )
                                    warehouse_error = (
                                        payload.get("warehouse_logistics_error")
                                        or payload.get("warehouse_logistics_status")
                                        or "-"
                                    )
                                    payload["message"] = (
                                        "仓库物流处理失败：" + str(warehouse_error)
                                    )
                                    payload["message"] = append_runtime_safety_notes(
                                        payload["message"],
                                        folder_write_enabled=create_folder,
                                        dedupe_write_enabled=write_dedupe,
                                    )
                                    await close_order_detail_dialog(page)
                                    return payload
                            else:
                                payload["status"] = "updated_folder_created_instruction_remark_failed"
                                instruction_error = (
                                    payload.get("instruction_remark_error") or payload.get("instruction_remark_status") or "-"
                                )
                                payload["message"] = (
                                    "说明书备注处理失败：" + str(instruction_error)
                                )
                                payload["message"] = append_runtime_safety_notes(
                                    payload["message"],
                                    folder_write_enabled=create_folder,
                                    dedupe_write_enabled=write_dedupe,
                                )
                                await close_order_detail_dialog(page)
                                return payload
                        else:
                            payload["status"] = "updated_folder_created_package_split_failed"
                            package_error = payload.get("package_split_error") or payload.get("package_split_status") or "-"
                            payload["message"] = (
                                "拆单处理失败：" + str(package_error)
                            )
                            payload["message"] = append_runtime_safety_notes(
                                payload["message"],
                                folder_write_enabled=create_folder,
                                dedupe_write_enabled=write_dedupe,
                            )
                            await close_order_detail_dialog(page)
                            return payload
                    else:
                        payload["status"] = "updated_folder_created_sku_failed"
                        payload["message"] = (
                            "SKU 调整失败："
                            f"{payload.get('sku_adjustment_error') or payload.get('sku_adjustment_status') or '-'}"
                        )
                        payload["message"] = append_runtime_safety_notes(
                            payload["message"],
                            folder_write_enabled=create_folder,
                            dedupe_write_enabled=write_dedupe,
                        )
                        await close_order_detail_dialog(page)
                        return payload
                else:
                    payload["status"] = "updated"
                    payload["message"] = (
                        "联系方式此前已完成，本轮跳过写回；文件夹和定制文件已完成，已加入最终完成列表。"
                        if contact_writeback_already_done
                        else "定制化 JSON 中没有电话/邮箱，本次不写回联系方式；文件夹和定制文件已完成，已加入最终完成列表。"
                        if skip_contact_writeback
                        else build_writeback_success_message(selected_contact)
                    )
                payload["message"] = adapt_completion_message_for_runtime(
                    payload["message"],
                    folder_write_enabled=create_folder,
                    dedupe_write_enabled=write_dedupe,
                )
                if folder_result.status == FOLDER_EXISTING_PLATFORM_ORDER:
                    payload["message"] = f"{payload['message']} 当月已有该平台单号文件夹：{folder_result.folder_path}"
                payload["message"] = append_runtime_safety_notes(
                    payload["message"],
                    folder_write_enabled=create_folder,
                    dedupe_write_enabled=write_dedupe,
                )
            else:
                payload["status"] = "updated_folder_created_zip_failed"
                payload["message"] = (
                    "定制文件生成失败："
                    f"{finalize_result.get('custom_zip_status') or '-'}"
                )
        else:
            payload["status"] = "updated_folder_failed"
            payload["message"] = (
                "文件夹生成失败："
                f"{format_folder_failure_reason(folder_result)}"
            )
    else:
        payload["status"] = "contact_write_blocked" if contact_guard_blocked else "needs_manual_save"
        payload["updated_system_order_nos"] = []
        payload["message"] = (
            message if contact_guard_blocked else f"联系方式保存失败：{message}"
        )
    payload["timings"]["total_ms"] = round(
        (time.monotonic() - item_started) * 1000
    )
    if api_order_context is None or payload.get("contact_browser_search_count"):
        await close_order_detail_dialog(page)
    return payload


async def process_batch_order_item(
    page,
    item: BatchOrderItem,
    amazon_quantity_client: AmazonOrderQuantityClient,
    dedupe_path: str | Path | None = None,
    payment_window_hours: float = DEFAULT_PAYMENT_WINDOW_HOURS,
    search_timeout_sec: int = 20,
    folder_root: str | Path | None = None,
    folder_date: str | None = None,
    create_folder: bool = True,
    download_custom_zip: bool = True,
    allow_sku_adjustment_page_write: bool | None = None,
    allow_package_split_page_write: bool | None = None,
    ignore_dedupe: bool = False,
    ignore_payment_window: bool = False,
    write_dedupe: bool = True,
    api_operations: CustomOrderApiOperations | None = None,
    interaction_policy: CustomOrderInteractionPolicy | None = None,
    log_dir: str | Path = "logs",
    validated_search_context: ValidatedOrderSearchContext | None = None,
    api_order_context: CustomOrderApiContext | None = None,
) -> dict[str, Any]:
    """Process one order and close any initialized detail on every exit path."""

    try:
        return await _process_batch_order_item_impl(
            page,
            item,
            amazon_quantity_client,
            dedupe_path=dedupe_path,
            payment_window_hours=payment_window_hours,
            search_timeout_sec=search_timeout_sec,
            folder_root=folder_root,
            folder_date=folder_date,
            create_folder=create_folder,
            download_custom_zip=download_custom_zip,
            allow_sku_adjustment_page_write=allow_sku_adjustment_page_write,
            allow_package_split_page_write=allow_package_split_page_write,
            ignore_dedupe=ignore_dedupe,
            ignore_payment_window=ignore_payment_window,
            write_dedupe=write_dedupe,
            api_operations=api_operations,
            interaction_policy=interaction_policy,
            log_dir=log_dir,
            validated_search_context=validated_search_context,
            api_order_context=api_order_context,
        )
    finally:
        underlying_page = (
            page.page if isinstance(page, _LazyContactOrderPage) else page
        )
        if underlying_page is not None and callable(
            getattr(underlying_page, "evaluate", None)
        ):
            await close_order_detail_dialog(page)


async def save_screenshot(page, log_dir: Path, prefix: str) -> str:
    """保存当前页面截图，用于失败排查和批量日志追踪。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.png"
    await page.screenshot(path=str(path), full_page=True)
    return str(path)

def write_result(log_dir: Path, result: SyncResult, contact: ContactInfo | None = None, texts: list[str] | None = None) -> str:
    """写入单次同步结果 JSON，保留本次执行明细。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"result_{time.strftime('%Y%m%d_%H%M%S')}.json"
    result.result_file = str(path)
    payload: dict[str, Any] = {"result": asdict(result)}
    if contact:
        payload["contact"] = asdict(contact)
    if texts is not None:
        payload["candidate_texts"] = texts[:30]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)



def _short_text(value: Any, limit: int = 500) -> str:
    """截断长文本并清理空白，生成适合日志展示的摘要。"""
    text = normalize_text(str(value or ""))
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def build_detail_text_preview(texts: list[str], *, limit: int = 8, width: int = 500) -> list[str]:
    """生成缺少联系方式诊断用的详情文本摘要。"""
    preview: list[str] = []
    seen: set[str] = set()
    for text in texts:
        shortened = _short_text(text, width)
        if not shortened or shortened in seen:
            continue
        seen.add(shortened)
        preview.append(shortened)
        if len(preview) >= limit:
            break
    return preview


def contact_writeback_fields(contact: ContactInfo) -> list[str]:
    """提取联系方式写回相关字段，供结果消息和日志复用。"""
    fields: list[str] = []
    if contact.phone:
        fields.append("电话")
    if contact.email:
        fields.append("买家邮箱")
    return fields


def build_writeback_success_message(contact: ContactInfo) -> str:
    """生成联系方式写回成功后的用户提示消息。"""
    fields = contact_writeback_fields(contact)
    missing_fields = missing_contact_fields(contact)
    field_text = "、".join(fields) if fields else "无字段"
    if missing_fields:
        return (
            f"成功写回：{field_text}。缺少 {'、'.join(missing_fields)}，"
            "但用户已确认部分写入；文件夹和定制文件已完成，已加入最终完成列表，后续不再巡检。"
        )
    return "已校验平台单号/系统单号并成功写回：电话、买家邮箱；文件夹和定制文件已完成，已加入最终完成列表，后续不再巡检。"


def build_writeback_without_processed_message(contact: ContactInfo) -> str:
    """兼容旧调用方，仅返回联系方式阶段本身的简短结果。"""
    fields = contact_writeback_fields(contact)
    field_text = "、".join(fields) if fields else "无字段"
    return f"联系方式处理完成：{field_text}。"


def build_candidate_debug_summary(debug: dict[str, Any]) -> dict[str, Any]:
    """压缩候选订单调试信息，避免批量日志过长。"""
    selected = debug.get("selected_table") or {}
    return compact_candidate_debug_summary(
        {
            "scan_log_file": debug.get("scan_log_file"),
            "recent_threshold": debug.get("recent_threshold"),
            "payment_window_hours": debug.get("payment_window_hours"),
            "detected_headers": debug.get("detected_headers") or selected.get("headers") or [],
            "column_indexes": debug.get("column_indexes") or selected.get("column_indexes") or {},
            "scan_summary": debug.get("scan_summary") or {},
            "skip_counts": debug.get("skip_counts") or {},
            "warnings": debug.get("warnings") or [],
            "unknown_asins": debug.get("unknown_asins") or [],
            "orders_to_update": debug.get("orders_to_update") or [],
        }
    )


def compact_candidate_debug_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """压缩已经生成过的候选调试摘要。"""

    orders_to_update = [
        _compact_scan_order(order)
        for order in (summary.get("orders_to_update") or [])
        if isinstance(order, Mapping)
    ]
    return {
        "scan_log_file": summary.get("scan_log_file"),
        "recent_threshold": summary.get("recent_threshold"),
        "payment_window_hours": summary.get("payment_window_hours"),
        "detected_headers": summary.get("detected_headers") or [],
        "column_indexes": summary.get("column_indexes") or {},
        "scan_summary": summary.get("scan_summary") or {},
        "skip_counts": summary.get("skip_counts") or {},
        "warnings": summary.get("warnings") or [],
        "unknown_asins": [
            _compact_unknown_asin_entry(item)
            for item in (summary.get("unknown_asins") or [])
            if isinstance(item, Mapping)
        ],
        "orders_to_update": orders_to_update,
    }


_LOG_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_LOG_LABELED_SECRET_RE = re.compile(
    r"(?i)(access[_ -]?token|refresh[_ -]?token|token|secret|password|authorization|cookie)"
    r"\s*[:：=]\s*([^\s,;，；]+)"
)
_LOG_SECRET_KEY_PARTS = (
    "accesstoken",
    "refreshtoken",
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
)


def _redact_log_text(value: Any) -> str:
    """Keep business diagnostics verbatim while never persisting credentials."""

    text = _LOG_BEARER_RE.sub("Bearer <redacted-secret>", str(value))
    return _LOG_LABELED_SECRET_RE.sub(
        lambda match: f"{match.group(1)}=<redacted-secret>",
        text,
    )


def _redact_log_value(value: Any, *, key: str | None = None) -> Any:
    canonical_key = re.sub(r"[^a-z0-9]", "", str(key or "").casefold())
    if key and any(part in canonical_key for part in _LOG_SECRET_KEY_PARTS):
        return "<redacted-secret>"
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_log_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, str):
        return _redact_log_text(value)
    return copy.deepcopy(value)


def _compact_log_mapping(source: Mapping[str, Any], keys: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """按白名单复制日志字段，跳过空值。"""

    result: dict[str, Any] = {}
    for key in keys:
        if key not in source:
            continue
        value = source.get(key)
        if value in (None, "", [], {}):
            continue
        result[key] = _redact_log_value(value, key=key)
    return result


def _compact_text_value(value: Any, limit: int = 240) -> str:
    """日志用短文本，避免完整表格行和长消息撑大 JSON。"""

    return _short_text(_redact_log_text(value), limit)


def _compact_scan_order(order: Mapping[str, Any]) -> dict[str, Any]:
    """压缩扫描候选订单字段。"""

    return _compact_log_mapping(
        order,
        (
            "platform_order_no",
            "platform_order_id",
            "system_order_no",
            "payment_time",
            "paid_at",
            "asin",
            "asin_or_product_id",
            "matched_asin",
            "parent_asin",
            "product_type",
            "sku",
            "logistics",
            "tag_text",
            "source_page",
            "source_scroll_top",
        ),
    )


def _compact_unknown_asin_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """压缩未知 ASIN 的定位字段。"""

    return _compact_log_mapping(
        entry,
        (
            "asin",
            "platform_order_no",
            "system_order_no",
            "sku",
            "payment_time",
            "source_page",
            "source_scroll_top",
        ),
    )


def _compact_scan_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """压缩扫描行，仅保留定位和跳过/命中信息。"""

    compact = _compact_log_mapping(
        row,
        (
            "row",
            "row_index",
            "page",
            "screen",
            "source_page",
            "source_scroll_top",
            "platform_order_no",
            "platform_order_id",
            "system_order_no",
            "payment_time",
            "paid_at_text",
            "payment_status",
            "asin",
            "matched_asin",
            "unknown_asins",
            "parent_asin",
            "product_type",
            "sku",
            "tag_text",
            "hit",
            "skip_reason",
            "warning",
            "error",
        ),
    )
    if "row_text" in row:
        compact["row_text_preview"] = _compact_text_value(row.get("row_text"), 180)
    return compact


def _compact_scan_rows(rows: Any, *, limit: int = 25) -> list[dict[str, Any]]:
    """只保留命中、异常和少量跳过样例。"""

    if not isinstance(rows, list):
        return []
    selected: list[Mapping[str, Any]] = []
    skip_seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        reason = str(row.get("skip_reason") or "")
        is_interesting = bool(row.get("hit") or row.get("warning") or row.get("error")) or reason in {
            "candidate",
            "order_line_error",
            "custom_zip_error",
        }
        if is_interesting:
            selected.append(row)
        elif reason and reason not in skip_seen and len(skip_seen) < 8:
            skip_seen.add(reason)
            selected.append(row)
        if len(selected) >= limit:
            break
    return [_compact_scan_row(row) for row in selected]


def compact_batch_scan_log(debug: dict[str, Any]) -> dict[str, Any]:
    """生成精简版 batch_scan 日志，去掉浏览器调试大块。"""

    selected = debug.get("selected_table") if isinstance(debug.get("selected_table"), Mapping) else {}
    compact: dict[str, Any] = _compact_log_mapping(
        debug,
        (
            "scan_started_at",
            "scan_finished_at",
            "payment_window_hours",
            "recent_threshold",
            "skip_counts",
            "skip_preview",
            "warnings",
            "unknown_asins",
            "page_size_1000",
            "wait_for_visible_rows",
            "retry_exact_search_skipped_batch_preparation",
            "search_duration_ms",
            "visible_rows_wait_duration_ms",
            "retry_scan_duration_ms",
            "detected_headers",
            "column_indexes",
            "stopped_due_to_old_payment",
            "raw_item_count",
            "unique_raw_item_count",
            "scan_summary",
            "candidate_count",
            "needs_update_platform_orders",
            "scan_log_file",
        ),
    )
    compact["detected_headers"] = compact.get("detected_headers") or selected.get("headers") or []
    compact["column_indexes"] = compact.get("column_indexes") or selected.get("column_indexes") or {}
    if selected:
        compact["selected_table"] = _compact_log_mapping(
            selected,
            ("index", "score", "selector", "id", "className", "headers", "column_indexes", "row_count_visible"),
        )
    compact["orders_to_update"] = [
        _compact_scan_order(order)
        for order in (debug.get("orders_to_update") or [])
        if isinstance(order, Mapping)
    ]
    compact["unknown_asins"] = [
        _compact_unknown_asin_entry(item)
        for item in (debug.get("unknown_asins") or [])
        if isinstance(item, Mapping)
    ]
    compact["scan_rows"] = _compact_scan_rows(debug.get("scan_rows"))
    if debug.get("raw_item_preview"):
        compact["raw_item_preview"] = [
            _compact_scan_row(row)
            for row in (debug.get("raw_item_preview") or [])[:10]
            if isinstance(row, Mapping)
        ]
    return compact


_BATCH_RESULT_TOP_KEYS: tuple[str, ...] = (
    "started_at",
    "finished_at",
    "status",
    "mode",
    "retry_order",
    "message",
    "screenshot_file",
    "dedupe_path",
    "dedupe_write_enabled",
    "payment_window_hours",
    "processed_before",
    "candidate_count",
    "updated_count",
    "skipped_count",
    "scan_log_file",
    "result_file",
)


_BATCH_ITEM_BASE_KEYS: tuple[str, ...] = (
    "platform_order_no",
    "system_order_no",
    "system_order_nos",
    "source_system_order_no",
    "status",
    "message",
    "asin",
    "sku",
    "parent_asin",
    "product_type",
    "logistics",
    "paid_at",
    "payment_status",
    "recipient_name",
    "recipient_name_source",
    "phone",
    "email",
    "shipping_address_text",
    "writeback_fields",
    "missing_contact_fields",
    "source_excerpt",
    "updated_system_order_nos",
    "contact_writeback_already_done",
    "contact_writeback_skipped",
    "contact_writeback_skip_reason",
    "contact_writeback_recorded",
    "folder_status",
    "folder_path",
    "folder_name",
    "folder_name_full",
    "folder_components",
    "folder_components_full",
    "folder_name_was_shortened",
    "folder_name_removed_components",
    "folder_name_max_length",
    "full_folder_name_txt",
    "folder_error",
    "folder_missing_rule_title",
    "folder_missing_rule_value",
    "folder_missing_rule_line",
    "folder_warnings",
    "custom_zip_status",
    "custom_zip_count",
    "custom_zip_error",
    "custom_zip_warnings",
    "order_line_error",
    "order_line_warnings",
    "amazon_quantity_status",
    "amazon_quantity_error",
    "amazon_quantity_order_id",
    "amazon_quantity_asin",
    "amazon_quantity_sku",
    "amazon_quantity_item_count",
    "dedupe_final_recorded",
    "dedupe_write_skipped",
    "sku_adjustment_required",
    "sku_adjustment_workflow_kind",
    "sales_revenue_total",
    "sales_revenue_currency",
    "sales_revenue_status",
    "sales_revenue_source",
    "instruction_replaced_at",
    "sku_adjustment_already_done",
    "sku_adjustment_page_write_enabled",
    "sku_adjustment_status",
    "sku_adjustment_complete",
    "sku_adjustment_error",
    "sku_adjustment_plan_generated",
    "sku_adjustment_plan_only",
    "sku_adjustment_recorded",
    "package_split_required",
    "package_split_already_done",
    "package_split_page_write_enabled",
    "package_split_status",
    "package_split_complete",
    "package_split_error",
    "package_split_recorded",
    "package_split_system_order_nos",
    "instruction_remark_required",
    "instruction_remark_already_done",
    "instruction_remark_page_write_enabled",
    "instruction_remark_status",
    "instruction_remark_complete",
    "instruction_remark_error",
    "instruction_remark_recorded",
    "instruction_remark_target_system_order_no",
    "instruction_remark_customer_remark",
    "warehouse_logistics_required",
    "warehouse_logistics_already_done",
    "warehouse_logistics_page_write_enabled",
    "warehouse_logistics_status",
    "warehouse_logistics_complete",
    "warehouse_logistics_error",
    "warehouse_logistics_result_detail",
    "warehouse_logistics_recorded",
    "warehouse_logistics_postal_code",
    "warehouse_logistics_postal_source",
    "warehouse_logistics_postal_diagnostic",
    "warehouse_logistics_decisions",
    "warehouse_logistics_write_results",
    "warehouse_logistics_preview_ms",
    "warehouse_logistics_projection_source",
    "warehouse_logistics_projection_wait_after_approval_ms",
    "warehouse_logistics_projection_attempts",
    "warehouse_logistics_projection_waited_seconds",
    "shipping_postal_code",
    "shipping_postal_source",
    "shipping_postal_api_diagnostic",
    "shipping_postal_request_id",
    "screenshot_file",
    "timings",
)


_FAILURE_STATUSES: set[str] = {
    "error",
    "updated_folder_failed",
    "updated_folder_created_zip_failed",
    "updated_folder_created_package_split_failed",
    "updated_folder_created_instruction_remark_failed",
    "updated_folder_created_warehouse_logistics_failed",
    "needs_manual_save",
    "skipped",
    "folder_failed",
}


def _is_failure_item(item: Mapping[str, Any]) -> bool:
    """判断批量结果条目是否属于失败或需要人工继续处理。"""

    status = str(item.get("status") or "").strip().lower()
    if status in _FAILURE_STATUSES:
        return True
    if status and status != "updated":
        return True
    return bool(
        item.get("folder_error")
        or item.get("order_line_error")
        or item.get("custom_zip_error")
        or item.get("amazon_quantity_error")
        or item.get("sku_adjustment_error")
        or item.get("package_split_error")
        or item.get("instruction_remark_error")
        or item.get("warehouse_logistics_error")
    )


def _compact_update_messages(messages: Any) -> list[str]:
    if not isinstance(messages, list):
        return []
    compact: list[str] = []
    for message in messages[:5]:
        text = _compact_text_value(message, 360)
        if text:
            compact.append(text)
    return compact


def _compact_extracted_contacts(contacts: Any) -> list[dict[str, Any]]:
    if not isinstance(contacts, list):
        return []
    result: list[dict[str, Any]] = []
    for contact in contacts[:5]:
        if not isinstance(contact, Mapping):
            continue
        compact = _compact_log_mapping(
            contact,
            ("system_order_no", "recipient_name", "phone", "email", "missing_fields", "source_excerpt"),
        )
        result.append(compact)
    return result


def _compact_custom_zip_files(files: Any) -> list[dict[str, Any]]:
    if not isinstance(files, list):
        return []
    result: list[dict[str, Any]] = []
    for item in files[:8]:
        if isinstance(item, Mapping):
            result.append(
                _compact_log_mapping(
                    item,
                    ("row_index", "asin", "sku", "zip_filename", "order_item_id", "json_filename", "status", "error"),
                )
            )
    return result


def _compact_amazon_matched_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    result: list[dict[str, Any]] = []
    for item in items[:5]:
        if isinstance(item, Mapping):
            result.append(
                _compact_log_mapping(
                    item,
                    ("asin", "ASIN", "seller_sku", "SellerSKU", "quantity_ordered", "QuantityOrdered", "order_item_id", "OrderItemId"),
                )
            )
    return result


def _compact_order_folder_lines(lines: Any) -> list[dict[str, Any]]:
    if not isinstance(lines, list):
        return []
    result: list[dict[str, Any]] = []
    for line in lines[:8]:
        if isinstance(line, Mapping):
            result.append(
                _compact_log_mapping(
                    line,
                    ("asin", "sku", "parent_asin", "product_type", "quantity", "order_item_id", "source_index"),
                )
            )
    return result


def _compact_batch_item(item: Mapping[str, Any]) -> dict[str, Any]:
    compact = _compact_log_mapping(item, _BATCH_ITEM_BASE_KEYS)
    if "message" in compact:
        compact["message"] = _compact_text_value(compact["message"], 500)
    update_messages = _compact_update_messages(item.get("update_messages"))
    if update_messages:
        compact["update_messages"] = update_messages
    contacts = _compact_extracted_contacts(item.get("extracted_contacts"))
    if contacts:
        compact["extracted_contacts"] = contacts
    order_lines = _compact_order_folder_lines(item.get("order_folder_lines"))
    if order_lines:
        compact["order_folder_lines"] = order_lines
    matched_items = _compact_amazon_matched_items(item.get("amazon_quantity_matched_items"))
    if matched_items:
        compact["amazon_quantity_matched_items"] = matched_items
    if _is_failure_item(item):
        custom_zip_files = _compact_custom_zip_files(item.get("custom_zip_files"))
        if custom_zip_files:
            compact["custom_zip_files"] = custom_zip_files
        if item.get("customization_pairs"):
            compact["customization_pair_count"] = len(item.get("customization_pairs") or {})
    return compact


def compact_batch_result_log(payload: dict[str, Any]) -> dict[str, Any]:
    """生成精简版 batch_result 日志。"""

    compact = _compact_log_mapping(payload, _BATCH_RESULT_TOP_KEYS)
    compact["items"] = [
        _compact_batch_item(item)
        for item in (payload.get("items") or [])
        if isinstance(item, Mapping)
    ]
    debug = payload.get("candidate_debug")
    if isinstance(debug, dict):
        compact["candidate_debug_summary"] = build_candidate_debug_summary(debug)
    elif isinstance(payload.get("candidate_debug_summary"), Mapping):
        compact["candidate_debug_summary"] = compact_candidate_debug_summary(payload.get("candidate_debug_summary") or {})
    return compact


def write_batch_result(log_dir: Path, payload: dict[str, Any]) -> str:
    """写入批量巡检结果 JSON 文件。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"batch_result_{time.strftime('%Y%m%d_%H%M%S')}.json"
    payload["result_file"] = str(path)
    log_payload = compact_batch_result_log(payload)
    path.write_text(json.dumps(log_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def write_batch_scan_log(log_dir: Path, debug: dict[str, Any]) -> str:
    """写入批量扫描日志，记录每轮巡检的候选和处理结果。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"batch_scan_{time.strftime('%Y%m%d_%H%M%S')}.json"
    debug["scan_log_file"] = str(path)
    log_payload = compact_batch_scan_log(debug)
    path.write_text(json.dumps(log_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def print_batch_item_skip_notice(item_result: dict[str, Any]) -> None:
    """输出单个批量候选被跳过的原因和规则缺失明细。"""
    status = str(item_result.get("status") or "")
    if status == "updated":
        return
    print("\n[跳过订单]")
    print(f"平台单号：{item_result.get('platform_order_no') or '-'}")
    print(f"系统单号：{item_result.get('system_order_no') or '-'}")
    print(f"状态：{status}")
    print(f"原因：{item_result.get('message') or '-'}")
    if item_result.get("folder_status"):
        print(f"文件夹状态：{item_result.get('folder_status')}")
    for line in folder_rule_missing_lines_from_log(item_result):
        print(line)
    if item_result.get("custom_zip_status"):
        print(f"定制zip状态：{item_result.get('custom_zip_status')}")


def print_batch_table_debug(debug: dict[str, Any]) -> None:
    """打印批量订单列表调试表格，辅助定位页面识别问题。"""
    selected = debug.get("selected_table") or {}
    headers = debug.get("detected_headers") or selected.get("headers") or []
    indexes = debug.get("column_indexes") or selected.get("column_indexes") or {}
    orders = debug.get("orders_to_update") or []

    print("\n[表格识别] 当前订单表头：")
    if headers:
        for index, header in enumerate(headers):
            print(f"{index}: {header}")
    else:
        print("未读取到表头。")

    print("\n[列索引]")
    print(f"平台单号列 = {indexes.get('platform', -1)}")
    print(f"付款时间列 = {indexes.get('payment', -1)}")
    print(f"ASIN/商品ID列 = {indexes.get('asin', -1)}")
    if "tag" in indexes:
        print(f"标签列 = {indexes.get('tag', -1)}")
    if "logistics" in indexes:
        print(f"客选物流列 = {indexes.get('logistics', -1)}")

    # 当前可见行明细会刷屏，完整内容仍写入 batch_scan 日志；
    # CMD 只保留人工巡检最需要的表头、列索引和候选订单列表。
    print("\n[需要修改订单 list]")
    print(json.dumps(orders, ensure_ascii=False, indent=2))

    unknown_asins = debug.get("unknown_asins") or []
    if unknown_asins:
        print("\n[未识别ASIN]")
        printed: set[str] = set()
        shown = 0
        for entry in unknown_asins:
            if not isinstance(entry, dict):
                continue
            asin = str(entry.get("asin") or "").strip().upper()
            if not asin or asin in printed:
                continue
            printed.add(asin)
            shown += 1
            if shown <= 20:
                print(
                    "ASIN："
                    f"{asin}；平台单号：{entry.get('platform_order_no') or '-'}；"
                    f"系统单号：{entry.get('system_order_no') or '-'}；"
                    f"SKU：{entry.get('sku') or '-'}；"
                    f"付款时间：{entry.get('payment_time') or '-'}"
                )
        if len(printed) > 20:
            print(f"... 还有 {len(printed) - 20} 个未识别 ASIN 已省略，请查看 batch_scan 日志。")

    warnings = debug.get("warnings") or []
    for warning in warnings[:5]:
        print(f"扫描警告：{warning}")


def _dedupe_write_enabled(args: argparse.Namespace) -> bool:
    """集中读取查重写入开关，避免批量和安全重测各自散落判断。"""

    return not bool(getattr(args, "no_dedupe_write", False))


async def collect_retry_order_candidates(
    page,
    args: argparse.Namespace,
    processed: set[str],
    debug: dict[str, Any],
) -> RetryOrderCandidateSelection:
    """按平台单号搜索后，用批量表格读取逻辑构造一个重测候选。

    安全重测的目标是验证批量链路的真实行为，因此这里不走旧的详情页直达逻辑；
    同时放开标签、已完成状态和付款窗口，避免为了复测而手动改 ERP 或 JSON。
    """

    platform_order_no = str(getattr(args, "retry_order", "") or "").strip()
    debug["retry_order"] = platform_order_no
    debug["scan_started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    debug["payment_window_hours"] = args.batch_payment_hours
    debug["retry_overrides"] = {
        "ignore_tags": True,
        "ignore_processed": True,
        "ignore_payment_window": True,
    }
    scan_started = time.monotonic()
    # This is an exact platform-order search, not a broad batch scan.  The
    # order view has already proved that system/platform columns exist, while
    # forced retry deliberately ignores tag, ASIN, payment window and the
    # current page size.  Re-running the 1000-row pager and four-column scan
    # here only adds DOM work and can disturb the searched result.
    debug["retry_exact_search_skipped_batch_preparation"] = True
    search_started = time.monotonic()
    search_meta = await fill_order_search(page, platform_order_no, "platform")
    debug["search_meta"] = search_meta
    if not search_meta.get("search_validation_ok"):
        raise RuntimeError(str(search_meta.get("search_validation_message") or "平台单号搜索框校验失败。"))

    system_order_nos = await wait_for_orders_in_list(page, platform_order_no, "platform", args.search_timeout_sec)
    search_duration_ms = round((time.monotonic() - search_started) * 1000)
    debug["browser_search_count"] = 1
    debug["search_duration_ms"] = search_duration_ms
    debug["system_order_nos_after_search"] = system_order_nos
    visible_rows_started = time.monotonic()
    wait_result = await wait_for_visible_batch_order_rows(page, debug)
    debug["visible_rows_wait_duration_ms"] = round(
        (time.monotonic() - visible_rows_started) * 1000
    )
    debug["detected_headers"] = wait_result.get("headers") or []
    debug["column_indexes"] = wait_result.get("column_indexes") or {}
    rows = list(wait_result.get("rows") or [])
    raw_items = [row for row in rows if str(row.get("platform_order_no") or "") == platform_order_no]
    debug["raw_item_count"] = len(raw_items)
    debug["unique_raw_item_count"] = len({f"{item.get('system_order_no')}:{item.get('platform_order_no')}" for item in raw_items})
    debug["raw_item_preview"] = raw_items[:20]
    candidates = build_batch_candidates_from_rows(
        raw_items,
        processed,
        limit=1,
        payment_window_hours=args.batch_payment_hours,
        debug=debug,
        ignore_tags=True,
        ignore_processed=True,
        ignore_payment_window=True,
        force_retry_order_no=platform_order_no,
    )
    debug["scan_finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    debug["retry_scan_duration_ms"] = round(
        (time.monotonic() - scan_started) * 1000
    )
    debug["scan_summary"] = {
        "read_total_unique_rows": len(raw_items),
        "raw_recent_unprocessed_rows": len(raw_items),
        "unique_raw_item_count": debug["unique_raw_item_count"],
        "candidate_count": len(candidates),
        "covered_recent_threshold": True,
        "warning_count": len(debug.get("warnings", [])),
    }
    debug["candidate_count"] = len(candidates)
    debug["orders_to_update"] = [
        {
            "platform_order_id": item.platform_order_no,
            "platform_order_no": item.platform_order_no,
            "system_order_no": item.system_order_no,
            "payment_time": item.paid_at_text,
            "asin_or_product_id": item.asin,
            "parent_asin": item.parent_asin,
            "matched_tent_asins": item.matched_asins,
            "all_asins": item.all_asins,
            "sku": item.sku,
            "source_page": item.source_page,
            "source_scroll_top": item.source_scroll_top,
        }
        for item in candidates
    ]
    return RetryOrderCandidateSelection(
        candidates=tuple(candidates),
        search_context=ValidatedOrderSearchContext(
            order_no=platform_order_no,
            search_kind="platform",
            system_order_nos=tuple(dict.fromkeys(system_order_nos)),
            search_meta=dict(search_meta),
            search_duration_ms=search_duration_ms,
        ),
    )


async def process_batch_candidate_with_policy(
    page,
    item: BatchOrderItem,
    amazon_quantity_client: AmazonOrderQuantityClient,
    args: argparse.Namespace,
    processed: set[str],
    *,
    ignore_dedupe: bool = False,
    validated_search_context: ValidatedOrderSearchContext | None = None,
) -> tuple[dict[str, Any], bool]:
    """复用批量单项处理，并在一个地方处理最终查重写入策略。"""

    dedupe_write_enabled = _dedupe_write_enabled(args)
    if not ignore_dedupe:
        latest_processed = load_processed_platform_orders(args.dedupe_path)
        if item.platform_order_no in latest_processed:
            return (
                {
                    "platform_order_no": item.platform_order_no,
                    "system_order_no": item.system_order_no,
                    "status": "already_processed",
                    "message": "平台单号已在最终完成列表中，进入详情前跳过。",
                },
                False,
            )

    item_result = await process_batch_order_item(
        page,
        item,
        amazon_quantity_client,
        dedupe_path=args.dedupe_path,
        payment_window_hours=args.batch_payment_hours,
        search_timeout_sec=args.search_timeout_sec,
        folder_root=args.folder_root,
        log_dir=args.log_dir,
        folder_date=args.folder_date,
        create_folder=not args.no_create_folder,
        download_custom_zip=not args.no_download_custom_zip,
        allow_sku_adjustment_page_write=(
            (not args.no_create_folder) or bool(getattr(args, "allow_sku_adjustment", False))
        ),
        allow_package_split_page_write=(
            (not args.no_create_folder) or bool(getattr(args, "allow_package_split", False))
        ),
        ignore_dedupe=ignore_dedupe,
        ignore_payment_window=bool(getattr(args, "retry_order", None)),
        write_dedupe=dedupe_write_enabled,
        api_operations=getattr(args, "custom_order_api_operations", None),
        interaction_policy=getattr(args, "custom_order_interaction_policy", None),
        validated_search_context=validated_search_context,
        api_order_context=getattr(args, "custom_order_api_context", None),
    )
    if item_result.get("status") != "updated":
        return item_result, False

    dedupe_system_order_no = item_result.get("source_system_order_no") or item_result.get("system_order_no") or item.system_order_no
    final_recorded = append_final_processed_if_allowed(
        args.dedupe_path,
        item.platform_order_no,
        str(dedupe_system_order_no),
        write_enabled=dedupe_write_enabled and not args.no_create_folder,
        product_type=item_result.get("product_type") or item.product_type,
        sku_adjustment_required=bool(item_result.get("sku_adjustment_required")),
    )
    item_result["dedupe_final_recorded"] = final_recorded
    if final_recorded:
        processed.add(item.platform_order_no)
    elif dedupe_write_enabled and not args.no_create_folder:
        item_result["status"] = "final_state_not_recorded"
        item_result["message"] = (
            "各业务步骤已返回成功，但服务器工作流未达到最终完成条件；"
            "已保留待处理状态，避免误报完成。"
        )
        return item_result, False
    elif args.no_create_folder or not dedupe_write_enabled:
        item_result["dedupe_write_skipped"] = True
    return item_result, True


async def run_batch_round(page, args: argparse.Namespace, log_dir: Path) -> dict[str, Any]:
    """执行一轮批量巡检，筛选候选订单并逐单处理。"""
    dedupe_write_enabled = _dedupe_write_enabled(args)
    if dedupe_write_enabled:
        migrate_dedupe_file(args.dedupe_path)
    processed = load_processed_platform_orders(args.dedupe_path)
    amazon_quantity_client = AmazonOrderQuantityClient.from_env(configuration_source_from_args(args))
    await close_order_detail_dialog(page)
    await ensure_order_view_mode(page, debug_dir=getattr(args, "debug_log_dir", "debug/logs"))
    candidate_debug: dict[str, Any] = {}
    try:
        candidates = await collect_batch_order_candidates(
            page,
            processed,
            limit=args.batch_limit,
            payment_window_hours=args.batch_payment_hours,
            debug=candidate_debug,
        )
        scan_log_file = write_batch_scan_log(log_dir, candidate_debug)
    except Exception as exc:
        screenshot_file = None
        try:
            screenshot_file = await save_screenshot(page, log_dir, "batch_collect_error")
        except Exception:
            pass
        scan_log_file = write_batch_scan_log(log_dir, candidate_debug)
        payload: dict[str, Any] = {
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "error",
            "message": str(exc),
            "screenshot_file": screenshot_file,
            "dedupe_path": str(Path(args.dedupe_path).resolve()),
            "dedupe_write_enabled": dedupe_write_enabled,
            "payment_window_hours": args.batch_payment_hours,
            "processed_before": len(processed),
            "candidate_count": 0,
            "updated_count": 0,
            "skipped_count": 0,
            "scan_log_file": scan_log_file,
            "candidate_debug": candidate_debug,
            "items": [],
        }
        write_batch_result(log_dir, payload)
        print_batch_table_debug(candidate_debug)
        return payload
    print_batch_table_debug(candidate_debug)
    payload: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "completed",
        "dedupe_path": str(Path(args.dedupe_path).resolve()),
        "dedupe_write_enabled": dedupe_write_enabled,
        "payment_window_hours": args.batch_payment_hours,
        "processed_before": len(processed),
        "candidate_count": len(candidates),
        "updated_count": 0,
        "skipped_count": 0,
        "scan_log_file": scan_log_file,
        "candidate_debug": candidate_debug,
        "items": [],
    }

    for item in candidates:
        try:
            item_result, updated = await process_batch_candidate_with_policy(
                page,
                item,
                amazon_quantity_client,
                args,
                processed,
            )
            if updated:
                payload["updated_count"] += 1
            else:
                payload["skipped_count"] += 1
                print_batch_item_skip_notice(item_result)
            payload["items"].append(item_result)
        except Exception as exc:
            payload["skipped_count"] += 1
            screenshot_file = None
            try:
                screenshot_file = await save_screenshot(page, log_dir, "batch_item_error")
            except Exception:
                pass
            error_item = {
                "platform_order_no": item.platform_order_no,
                "system_order_no": item.system_order_no,
                "status": "error",
                "message": str(exc),
                "screenshot_file": screenshot_file,
            }
            print_batch_item_skip_notice(error_item)
            payload["items"].append(error_item)
            try:
                await close_order_detail_dialog(page)
            except Exception:
                pass

    payload["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_batch_result(log_dir, payload)
    return payload


async def run_retry_order_round(page, args: argparse.Namespace, log_dir: Path) -> dict[str, Any]:
    """安全重测的一轮执行：复用批量巡检链路，但只处理指定平台单号。"""

    dedupe_write_enabled = _dedupe_write_enabled(args)
    if dedupe_write_enabled:
        migrate_dedupe_file(args.dedupe_path)
    processed = load_processed_platform_orders(args.dedupe_path)
    amazon_quantity_client = AmazonOrderQuantityClient.from_env(configuration_source_from_args(args))
    candidate_debug: dict[str, Any] = {}
    try:
        api_operations = getattr(args, "custom_order_api_operations", None)
        if api_operations is not None:
            expected_system_order_no = str(
                getattr(args, "retry_system_order_no", "") or ""
            ).strip()
            if not expected_system_order_no:
                workflow_record = load_order_workflow_record(
                    args.dedupe_path,
                    str(getattr(args, "retry_order", "") or ""),
                ) or {}
                expected_system_order_no = str(
                    workflow_record.get("system_order_no") or ""
                ).strip()
            if not expected_system_order_no:
                raise RuntimeError("API 定制订单处理缺少预期系统单号。")
            api_context = await api_operations.get_order_context(
                platform_order_no=str(getattr(args, "retry_order", "") or ""),
                system_order_no=expected_system_order_no,
            )
            args.custom_order_api_context = api_context
            candidate_debug.update(
                {
                    "candidate_source": "lingxing_openapi",
                    "browser_search_count": 0,
                    "candidate_count": 1,
                    "system_order_nos": list(api_context.system_order_nos),
                    "sales_revenue_total": api_context.item.sales_revenue_total,
                    "sales_revenue_currency": api_context.item.sales_revenue_currency,
                    "sales_revenue_status": api_context.item.sales_revenue_status,
                    "sales_revenue_source": api_context.item.sales_revenue_source,
                }
            )
            selection = RetryOrderCandidateSelection(
                candidates=(api_context.item,),
                search_context=ValidatedOrderSearchContext(
                    order_no=api_context.item.platform_order_no,
                    search_kind="platform",
                    system_order_nos=api_context.system_order_nos,
                    search_meta={
                        "search_validation_ok": True,
                        "search_source": "lingxing_openapi",
                    },
                    search_duration_ms=0,
                    browser_search_count=0,
                ),
            )
        else:
            await close_order_detail_dialog(page)
            await ensure_order_view_mode(
                page,
                debug_dir=getattr(args, "debug_log_dir", "debug/logs"),
            )
            selection = await collect_retry_order_candidates(
                page,
                args,
                processed,
                candidate_debug,
            )
        candidates = list(selection.candidates)
        scan_log_file = write_batch_scan_log(log_dir, candidate_debug)
    except Exception as exc:
        screenshot_file = None
        if api_operations is None:
            try:
                screenshot_file = await save_screenshot(page, log_dir, "retry_collect_error")
            except Exception:
                pass
        scan_log_file = write_batch_scan_log(log_dir, candidate_debug)
        payload: dict[str, Any] = {
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "error",
            "mode": "safe_retry",
            "retry_order": getattr(args, "retry_order", None),
            "message": str(exc),
            "screenshot_file": screenshot_file,
            "dedupe_path": str(Path(args.dedupe_path).resolve()),
            "dedupe_write_enabled": dedupe_write_enabled,
            "payment_window_hours": args.batch_payment_hours,
            "processed_before": len(processed),
            "candidate_count": 0,
            "updated_count": 0,
            "skipped_count": 0,
            "scan_log_file": scan_log_file,
            "candidate_debug": candidate_debug,
            "items": [],
        }
        write_batch_result(log_dir, payload)
        print_batch_table_debug(candidate_debug)
        return payload

    print_batch_table_debug(candidate_debug)
    payload: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "completed",
        "mode": "safe_retry",
        "retry_order": getattr(args, "retry_order", None),
        "dedupe_path": str(Path(args.dedupe_path).resolve()),
        "dedupe_write_enabled": dedupe_write_enabled,
        "payment_window_hours": args.batch_payment_hours,
        "processed_before": len(processed),
        "candidate_count": len(candidates),
        "updated_count": 0,
        "skipped_count": 0,
        "scan_log_file": scan_log_file,
        "candidate_debug": candidate_debug,
        "items": [],
    }

    if not candidates:
        payload.update(retry_no_candidate_outcome(candidate_debug))
        payload["skipped_count"] = 1
    else:
        item = candidates[0]
        try:
            item_result, updated = await process_batch_candidate_with_policy(
                page,
                item,
                amazon_quantity_client,
                args,
                processed,
                # The CLI safe-retry command intentionally replays all
                # stages.  The desktop's normal processing action opts into
                # durable stage-resume so completed SQLite stages are never
                # written twice after a partial or interrupted run.
                ignore_dedupe=not bool(
                    getattr(args, "resume_workflow_stages", False)
                ),
                validated_search_context=selection.search_context,
            )
            if updated:
                payload["updated_count"] += 1
            else:
                payload["skipped_count"] += 1
                print_batch_item_skip_notice(item_result)
            payload["items"].append(item_result)
        except Exception as exc:
            payload["skipped_count"] += 1
            screenshot_file = None
            if getattr(args, "custom_order_api_operations", None) is None:
                try:
                    screenshot_file = await save_screenshot(page, log_dir, "retry_item_error")
                except Exception:
                    pass
            error_item = {
                "platform_order_no": item.platform_order_no,
                "system_order_no": item.system_order_no,
                "status": "error",
                "message": str(exc),
                "screenshot_file": screenshot_file,
            }
            print_batch_item_skip_notice(error_item)
            payload["items"].append(error_item)
            if getattr(args, "custom_order_api_operations", None) is None:
                try:
                    await close_order_detail_dialog(page)
                except Exception:
                    pass

    payload["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_batch_result(log_dir, payload)
    return payload


async def run_retry_order(args: argparse.Namespace) -> dict[str, Any]:
    """安全重测入口：API 路径仅在联系方式写回时延迟创建网页会话。"""

    log_dir = Path(args.log_dir).resolve()
    if getattr(args, "custom_order_api_operations", None) is not None:
        page = _LazyContactOrderPage(args)
        try:
            payload = await run_retry_order_round(page, args, log_dir)
            print_batch_round_summary(payload)
            return payload
        finally:
            await page.close()

    login_config = LoginConfig()
    if not args.no_auto_login:
        login_config = load_login_config(configuration_source_from_args(args))
    playwright, context = await launch_context(args)
    page = await get_first_page(context)
    last_payload: dict[str, Any] = {}
    try:
        if "mpOrderManagement" not in page.url:
            await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
        await wait_for_order_page(
            page,
            args.login_timeout_sec,
            login_config,
            auto_login=not args.no_auto_login,
            debug_dir=getattr(args, "debug_log_dir", "debug/logs"),
        )
        if "mpOrderManagement" not in page.url:
            await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
            await wait_for_order_page(
                page,
                args.login_timeout_sec,
                login_config,
                auto_login=not args.no_auto_login,
                debug_dir=getattr(args, "debug_log_dir", "debug/logs"),
            )
        last_payload = await run_retry_order_round(page, args, log_dir)
        print_batch_round_summary(last_payload)
        return last_payload
    finally:
        if args.keep_browser_open:
            print("Browser will stay open for inspection.")
        else:
            await context.close()
        await playwright.stop()
    return last_payload


async def run_batch(args: argparse.Namespace) -> dict[str, Any]:
    """持续运行批量巡检主循环，并按间隔等待下一轮。"""
    log_dir = Path(args.log_dir).resolve()
    login_config = LoginConfig()
    if not args.no_auto_login:
        login_config = load_login_config(configuration_source_from_args(args))
    playwright, context = await launch_context(args)
    page = await get_first_page(context)
    last_payload: dict[str, Any] = {}
    try:
        await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
        await wait_for_order_page(
            page,
            args.login_timeout_sec,
            login_config,
            auto_login=not args.no_auto_login,
            debug_dir=getattr(args, "debug_log_dir", "debug/logs"),
        )
        while True:
            if "mpOrderManagement" not in page.url:
                await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
                await wait_for_order_page(
                    page,
                    args.login_timeout_sec,
                    login_config,
                    auto_login=not args.no_auto_login,
                    debug_dir=getattr(args, "debug_log_dir", "debug/logs"),
                )
            try:
                last_payload = await run_batch_round(page, args, log_dir)
            except Exception as exc:
                screenshot_file = None
                try:
                    screenshot_file = await save_screenshot(page, log_dir, "batch_round_error")
                except Exception:
                    pass
                last_payload = {
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "error",
                    "message": str(exc),
                    "screenshot_file": screenshot_file,
                    "candidate_count": 0,
                    "updated_count": 0,
                    "skipped_count": 0,
                    "items": [],
                }
                write_batch_result(log_dir, last_payload)
            print_batch_round_summary(last_payload)
            if not args.loop:
                return last_payload
            await wait_before_next_round(page, args)
            try:
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)
            except Exception:
                await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
    finally:
        if args.keep_browser_open or args.loop:
            print("Browser will stay open for inspection.")
        else:
            await context.close()
        await playwright.stop()
    return last_payload


async def run_once(args: argparse.Namespace) -> SyncResult:
    """执行单个订单的搜索、读取、写回和文件夹处理流程。"""
    log_dir = Path(args.log_dir).resolve()
    result = SyncResult(
        system_order_no=None,
        searched_order_no=args.order_no,
        search_kind="visible",
        phone=None,
        email=None,
        dry_run=not bool(args.apply),
        status="failed",
        message="Not completed.",
    )
    try:
        search_kind = guess_search_kind(args.order_no, args.search_kind)
        result.search_kind = search_kind
    except ValueError as exc:
        result.message = str(exc)
        write_result(log_dir, result)
        return result

    login_config = LoginConfig()
    if not args.no_auto_login:
        login_config = load_login_config(configuration_source_from_args(args))
    amazon_quantity_client = AmazonOrderQuantityClient.from_env(configuration_source_from_args(args))
    playwright, context = await launch_context(args)
    page = await get_first_page(context)
    contact: ContactInfo | None = None
    texts: list[str] = []

    try:
        await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
        await wait_for_order_page(
            page,
            args.login_timeout_sec,
            login_config,
            auto_login=not args.no_auto_login,
            debug_dir=getattr(args, "debug_log_dir", "debug/logs"),
        )

        if args.order_no:
            search_meta = await fill_order_search(page, args.order_no, search_kind)
            result.selected_search_type = search_meta.get("selected_search_type")
            result.search_input_value = search_meta.get("search_input_value")
            result.search_validation_message = search_meta.get("search_validation_message")
            if not search_meta.get("search_validation_ok"):
                result.screenshot_file = await save_screenshot(page, log_dir, "search_input_error")
                raise RuntimeError(str(result.search_validation_message or "Order search input validation failed."))
            system_order_nos = await wait_for_orders_in_list(page, args.order_no, search_kind, args.search_timeout_sec)
            if not system_order_nos:
                raise RuntimeError(f"No target order was found after search: {args.order_no}.")
        else:
            system_order_no = await find_visible_system_order_no(page, None)
            system_order_nos = [system_order_no] if system_order_no else []

        if not system_order_nos:
            raise RuntimeError("当前订单列表中未找到系统单号。")
        result.system_order_nos = system_order_nos

        source_system_order_no, contact, texts = await find_contact_from_system_orders(page, system_order_nos)
        if source_system_order_no:
            result.source_system_order_no = source_system_order_no
            result.system_order_no = source_system_order_no
        else:
            result.system_order_no = system_order_nos[0]
            contact = contact or ContactInfo(phone=None, email=None, source_count=0, source_excerpt="")
        result.phone = contact.phone
        result.email = contact.email

        single_folder_item: BatchOrderItem | None = None
        single_folder_context: dict[str, Any] = {}
        single_shipping_address_text = ""
        single_payment_time = latest_payment_text("\n".join(texts))
        try:
            identity = await assert_current_detail_order(
                page,
                result.system_order_no or system_order_nos[0],
                args.order_no if search_kind == "platform" else None,
                "before folder preview",
            )
            platform_order_no = args.order_no if search_kind == "platform" else None
            if not platform_order_no:
                platform_order_nos = [str(item) for item in identity.get("platform_order_nos") or []]
                platform_order_no = platform_order_nos[0] if platform_order_nos else None
            product_match = match_supported_product("\n".join(texts))
            if platform_order_no and product_match:
                single_folder_item = BatchOrderItem(
                    system_order_no=result.system_order_no or system_order_nos[0],
                    platform_order_no=platform_order_no,
                    row_text="\n".join(texts[:5]),
                    paid_at_text=single_payment_time,
                    asin=product_match.asin,
                    parent_asin=product_match.parent_asin,
                    product_type=product_match.product_type,
                )
                single_folder_context = await collect_order_folder_json_context(
                    page,
                    single_folder_item,
                    amazon_quantity_client,
                    single_folder_item.system_order_no,
                    staging_root=Path(args.log_dir) / "custom_zip_staging",
                    download_custom_zip=not args.no_download_custom_zip,
                    api_operations=getattr(args, "custom_order_api_operations", None),
                    interaction_policy=getattr(args, "custom_order_interaction_policy", None),
                )
                single_shipping_address_text = await read_detail_shipping_address_text(page)
                zip_bundle = single_folder_context.get("zip_bundle")
                json_contacts = extract_contact_candidates_from_json_items(
                    getattr(zip_bundle, "customization_items", []) if zip_bundle is not None else []
                )
                if json_contacts:
                    contact = json_contacts[0]
        except Exception:
            single_folder_item = None

        missing_fields = missing_contact_fields(contact)
        skip_single_contact_writeback = bool(
            single_folder_item and not (contact.phone or contact.email) and single_folder_context.get("zip_bundle") is not None
        )
        if missing_fields and not (contact.phone or contact.email) and not skip_single_contact_writeback:
            result.screenshot_file = await save_screenshot(page, log_dir, "no_contact_found")
            raise RuntimeError(
                f"已检查 {len(system_order_nos)} 个系统单号，但联系方式仍缺少：{'、'.join(missing_fields)}。"
            )

        if not args.apply:
            if single_folder_item:
                single_quantity_result = single_folder_context.get("amazon_quantity_result")
                order_lines = single_folder_context.get("order_lines") or []
                if not order_lines:
                    folder_preview = json_context_failure_folder_result(single_folder_context, folder_root=args.folder_root)
                else:
                    folder_preview = build_and_create_order_folder_from_lines(
                        order_item=single_folder_item,
                        order_lines=order_lines,
                        recipient_name=str(single_folder_context.get("recipient_name") or ""),
                        payment_time=single_payment_time,
                        folder_root=args.folder_root,
                        override_date=args.folder_date,
                        create_folder=False,
                        logistics=single_folder_item.logistics,
                        shipping_address_text=single_shipping_address_text,
                    )
                folder_preview_log = folder_preview.to_log_dict()
                if isinstance(single_quantity_result, AmazonOrderQuantityResult):
                    folder_preview_log.update(single_quantity_result.to_log_dict())
                zip_bundle = single_folder_context.get("zip_bundle")
                if zip_bundle is not None:
                    folder_preview_log.update(zip_bundle.to_log_dict())
                result.folder_preview = folder_preview_log
                result.folder_status = folder_preview.status
                result.folder_name = folder_preview.folder_name
                result.folder_path = folder_preview.folder_path
            result.status = "preview"
            missing_text = f"；缺少 {'、'.join(missing_fields)}" if missing_fields else ""
            result.message = (
                f"已从系统单号 {source_system_order_no} 解析联系方式；"
                f"共 {len(system_order_nos)} 个系统单号{missing_text}。预览模式不写回。"
            )
        else:
            if skip_single_contact_writeback:
                notify_no_contact_writeback_in_cmd(single_folder_item.platform_order_no, single_folder_item.system_order_no)
                updated = list(system_order_nos)
                update_messages = ["定制化 JSON 中没有电话/邮箱，本次不写回联系方式。"]
            else:
                expected_platform_order_no = args.order_no if search_kind == "platform" else None
                updated, update_messages = await update_contact_for_system_orders(
                    page,
                    system_order_nos,
                    contact,
                    expected_platform_order_no=expected_platform_order_no,
                    source_system_order_no=source_system_order_no,
                    confirm_callback=confirm_writeback_in_cmd,
                )
            result.updated_system_order_nos = updated
            result.update_messages = update_messages
            if len(updated) == len(system_order_nos):
                result.status = "updated"
                result.message = (
                    "定制化 JSON 中没有电话/邮箱，本次不写回联系方式；继续处理文件夹。"
                    if skip_single_contact_writeback
                    else f"已从系统单号 {source_system_order_no} 获取联系方式，并写回 {len(updated)} 个系统单号。"
                )
            else:
                result.status = "needs_manual_save"
                result.message = (
                    "联系方式保存失败："
                    f"仅写回 {len(updated)}/{len(system_order_nos)} 个系统单号。"
                )
                result.screenshot_file = await save_screenshot(page, log_dir, "needs_manual_save")

        if result.status == "updated" and single_folder_item:
            single_quantity_result = single_folder_context.get("amazon_quantity_result")
            order_lines = single_folder_context.get("order_lines") or []
            if not order_lines:
                folder_result = json_context_failure_folder_result(single_folder_context, folder_root=args.folder_root)
            else:
                folder_result = build_and_create_order_folder_from_lines(
                    order_item=single_folder_item,
                    order_lines=order_lines,
                    recipient_name=str(single_folder_context.get("recipient_name") or ""),
                    payment_time=single_payment_time,
                    folder_root=args.folder_root,
                    override_date=args.folder_date,
                    create_folder=False,
                    logistics=single_folder_item.logistics,
                    shipping_address_text=single_shipping_address_text,
                )
            if folder_result.status == FOLDER_EXISTING_PLATFORM_ORDER:
                notify_existing_folder_in_cmd(
                    single_folder_item.platform_order_no,
                    single_folder_item.system_order_no,
                    folder_result,
                    folder_write_enabled=not args.no_create_folder,
                    dedupe_write_enabled=not bool(getattr(args, "no_dedupe_write", False)),
                )
            elif not args.no_create_folder and folder_result.status in SUCCESS_FOLDER_STATUSES:
                if await confirm_folder_creation_in_cmd(single_folder_item.platform_order_no, single_folder_item.system_order_no, folder_result):
                    folder_result = create_order_folder_from_preview(
                        folder_result,
                        create_folder=True,
                        platform_order_no=single_folder_item.platform_order_no,
                    )
                else:
                    folder_result = cancel_folder_creation_result(folder_result)
            folder_result_log = folder_result.to_log_dict()
            if isinstance(single_quantity_result, AmazonOrderQuantityResult):
                folder_result_log.update(single_quantity_result.to_log_dict())
            zip_bundle = single_folder_context.get("zip_bundle")
            if zip_bundle is not None:
                folder_result_log.update(zip_bundle.to_log_dict())
            result.folder_preview = folder_result_log
            result.folder_status = folder_result.status
            result.folder_name = folder_result.folder_name
            result.folder_path = folder_result.folder_path
            if folder_result.status not in SUCCESS_FOLDER_STATUSES:
                result.status = "updated_folder_failed"
                result.message = (
                    "文件夹生成失败："
                    f"{format_folder_failure_reason(folder_result)}"
                )
            else:
                finalize_result = finalize_custom_zip_files_for_folder(
                    folder_result,
                    single_folder_context,
                    allow_folder_write=not args.no_create_folder,
                )
                result.custom_zip_status = str(finalize_result.get("custom_zip_status") or "")
                result.custom_zip_path = ", ".join(str(item) for item in finalize_result.get("custom_zip_copied_files") or []) or None
                if finalize_result.get("full_folder_name_txt"):
                    folder_result.full_folder_name_txt = str(finalize_result.get("full_folder_name_txt"))
                zip_ok = (
                    finalize_result.get("custom_zip_status") in {CUSTOM_ZIP_MOVED, CUSTOM_ZIP_DISABLED}
                    or args.no_create_folder
                )
                if not zip_ok:
                    result.status = "updated_folder_created_zip_failed"
                    result.message = (
                        "定制文件生成失败："
                        f"{finalize_result.get('custom_zip_status') or '-'}"
                    )
                elif folder_result.status == FOLDER_EXISTING_PLATFORM_ORDER:
                    result.message = f"{result.message} 当月已有该平台单号文件夹：{folder_result.folder_path}"
                result.message = append_runtime_safety_notes(
                    result.message,
                    folder_write_enabled=not args.no_create_folder,
                    dedupe_write_enabled=not bool(getattr(args, "no_dedupe_write", False)),
                )

        write_result(log_dir, result, contact=contact, texts=texts)
        return result
    except Exception as exc:
        result.message = str(exc)
        if not result.screenshot_file:
            try:
                result.screenshot_file = await save_screenshot(page, log_dir, "error")
            except Exception:
                pass
        write_result(log_dir, result, contact=contact, texts=texts)
        return result
    finally:
        if args.keep_browser_open:
            print("浏览器将保持打开，方便检查。")
        else:
            await context.close()
        await playwright.stop()

