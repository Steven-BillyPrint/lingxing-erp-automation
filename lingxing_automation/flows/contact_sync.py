from __future__ import annotations

import argparse
import asyncio
import copy
import json
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .batch_runtime import print_batch_round_summary, wait_before_next_round
from ..browser.session import get_first_page, launch_context, wait_for_order_page
from ..config import load_login_config
from ..constants import ORDER_MANAGEMENT_URL
from ..models import (
    BatchOrderItem,
    ContactInfo,
    FolderBuildResult,
    FolderNameShortenResult,
    LoginConfig,
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
    update_current_detail_contact,
    update_contact_for_system_orders,
    wait_for_detail,
)
from ..pages.order_management import (
    build_batch_candidates_from_rows,
    collect_batch_order_candidates,
    collect_visible_batch_order_rows,
    ensure_batch_key_columns_visible,
    ensure_page_size_1000,
    ensure_order_view_mode,
    fill_order_search,
    find_visible_system_order_no,
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
from ..parsers.orders import guess_search_kind
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
    AMAZON_QUANTITY_RESOLVED,
    AmazonOrderQuantityClient,
    AmazonOrderQuantityResult,
)
from ..services.custom_attachment_downloader import (
    CUSTOM_ZIP_SKIPPED_NO_FOLDER,
)
from ..services.custom_zip_downloader import CUSTOM_ZIP_DISABLED, download_order_custom_zip_bundle
from ..services.custom_zip_parser import (
    CUSTOM_ZIP_MOVED,
    cleanup_custom_zip_staging_dir,
    copy_custom_zip_files_to_folder,
    parse_order_custom_zip_bundle,
    write_full_folder_name_txt,
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
    execute_tent_sku_adjustment,
    read_detail_shipping_address_text,
    read_list_shipping_deadline_text,
)
from ..services.tent_sku_planner import build_tent_sku_plan, format_tent_sku_plan_for_cmd
from ..storage.dedupe import (
    append_contact_writeback_platform_order,
    append_folder_complete_platform_order,
    append_package_split_platform_order,
    append_processed_platform_order,
    append_sku_adjustment_platform_order,
    is_contact_writeback_done,
    is_folder_complete,
    is_package_split_done,
    is_platform_order_processed,
    is_sku_adjustment_done,
    load_processed_platform_orders,
    migrate_dedupe_file,
)


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
    return folder_result.status


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
    if getattr(zip_bundle, "status", None) == CUSTOM_ZIP_DISABLED:
        result["custom_zip_status"] = CUSTOM_ZIP_DISABLED
        return result
    if not allow_folder_write:
        result["custom_zip_error"] = "文件夹写入已关闭，跳过复制定制 zip。"
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


async def collect_order_folder_json_context(
    page,
    item: BatchOrderItem,
    amazon_quantity_client: AmazonOrderQuantityClient,
    system_order_no: str,
    *,
    staging_root: str | Path,
    download_custom_zip: bool,
) -> dict[str, Any]:
    """收集文件夹生成所需的 zip JSON、Amazon 数量和收件人信息。"""

    recipient_name = await read_detail_recipient_name(page)
    quantity_result = await amazon_quantity_client.get_order_items(item.platform_order_no)
    staging_dir = Path(staging_root) / item.platform_order_no
    if not download_custom_zip:
        zip_bundle = disabled_custom_zip_bundle(item)
        return {
            "recipient_name": recipient_name,
            "amazon_quantity_result": quantity_result,
            "zip_bundle": zip_bundle,
            "custom_zip_staging_dir": str(staging_dir),
            "order_lines": [],
            "order_line_warnings": [],
            "order_line_error": "custom_zip_disabled",
        }

    raw_bundle = await download_order_custom_zip_bundle(
        page,
        platform_order_no=item.platform_order_no,
        system_order_no=system_order_no,
        staging_root=staging_root,
        enabled=True,
        expected_zip_count=expected_custom_zip_count(quantity_result),
        expected_order_item_ids=expected_custom_zip_order_item_ids(quantity_result),
    )
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
    return {
        "recipient_name": recipient_name,
        "amazon_quantity_result": quantity_result,
        "zip_bundle": zip_bundle,
        "custom_zip_staging_dir": str(staging_dir),
        "order_lines": order_lines,
        "order_line_warnings": order_line_warnings,
        "order_line_error": order_line_error,
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
) -> bool:
    """持久化联系方式阶段完成状态；文件夹失败时下轮可直接补建。"""
    if not dedupe_path:
        return False
    append_contact_writeback_platform_order(
        dedupe_path,
        platform_order_no,
        system_order_no,
        contact_status=contact_status,
    )
    return True


def record_contact_writeback_if_allowed(
    dedupe_path: str | Path | None,
    platform_order_no: str,
    system_order_no: str | None,
    *,
    contact_status: str,
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
    return True


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
) -> bool:
    """记录帐篷 SKU 阶段完成；非帐篷订单不会调用这个函数。"""

    if not write_enabled or not dedupe_path:
        return False
    append_sku_adjustment_platform_order(
        dedupe_path,
        platform_order_no,
        system_order_no,
        sku_status=sku_status,
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
    )
    return True


def order_requires_tent_sku_adjustment(item: BatchOrderItem, order_lines: list[Any]) -> bool:
    """只有包含帐篷 ASIN 的订单才需要第三阶段 SKU 调整。"""

    if item.product_type == PRODUCT_TYPE_TENT:
        return True
    return any(getattr(line, "product_type", None) == PRODUCT_TYPE_TENT for line in order_lines)


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


async def run_tent_sku_adjustment_stage(
    page,
    item: BatchOrderItem,
    system_order_no: str,
    folder_result: FolderBuildResult,
    *,
    shipping_address_text: str,
    dedupe_path: str | Path | None,
    write_dedupe: bool,
    allow_page_write: bool,
    read_dedupe: bool = True,
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

    await close_order_detail_dialog(page)
    shipping_deadline_text = await read_list_shipping_deadline_text(
        page,
        system_order_no=system_order_no,
        platform_order_no=item.platform_order_no,
    )
    plan = build_tent_sku_plan(
        platform_order_no=item.platform_order_no,
        system_order_no=system_order_no,
        folder_components=folder_result.folder_components_full or folder_result.folder_components,
        destination_text=shipping_address_text,
        shipping_deadline_text=shipping_deadline_text,
        asin=item.asin,
        payment_time_text=item.paid_at_text,
        logistics_text=item.logistics,
    )
    payload.update(plan.to_log_dict())
    payload["shipping_deadline_text"] = shipping_deadline_text
    if plan.manual_required:
        if await confirm_manual_tent_sku_done_in_cmd(item.platform_order_no, system_order_no, plan.manual_reason):
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
        return payload
    if not await confirm_tent_sku_plan_in_cmd(plan):
        payload["sku_adjustment_status"] = "user_cancelled"
        payload["sku_adjustment_error"] = "用户取消 SKU 调整。"
        return payload

    result = await execute_tent_sku_adjustment(page, plan)
    payload.update(result.to_log_dict())
    if result.status == "sku_adjustment_complete":
        payload["sku_adjustment_complete"] = True
        payload["sku_adjustment_recorded"] = record_sku_adjustment_if_allowed(
            dedupe_path,
            item.platform_order_no,
            system_order_no,
            write_enabled=write_dedupe,
            sku_status="auto",
        )
    return payload


async def run_tent_package_split_stage(
    page,
    item: BatchOrderItem,
    system_order_no: str,
    folder_result: FolderBuildResult,
    *,
    shipping_address_text: str,
    dedupe_path: str | Path | None,
    write_dedupe: bool,
    allow_page_write: bool,
    read_dedupe: bool = True,
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
        return payload

    await close_order_detail_dialog(page)
    shipping_deadline_text = await read_list_shipping_deadline_text(
        page,
        system_order_no=system_order_no,
        platform_order_no=item.platform_order_no,
    )
    sku_plan = build_tent_sku_plan(
        platform_order_no=item.platform_order_no,
        system_order_no=system_order_no,
        folder_components=folder_result.folder_components_full or folder_result.folder_components,
        destination_text=shipping_address_text,
        shipping_deadline_text=shipping_deadline_text,
        asin=item.asin,
        payment_time_text=item.paid_at_text,
        logistics_text=item.logistics,
    )
    plan = build_tent_package_split_plan(sku_plan)
    payload.update(plan.to_log_dict())
    payload["package_split_shipping_deadline_text"] = shipping_deadline_text

    if plan.manual_required:
        if await confirm_manual_tent_package_split_done_in_cmd(plan):
            payload["package_split_status"] = "manual_complete"
            payload["package_split_complete"] = True
            payload["package_split_recorded"] = record_package_split_if_allowed(
                dedupe_path,
                item.platform_order_no,
                system_order_no,
                write_enabled=write_dedupe,
                package_status="manual",
                package_required=plan.required,
                system_order_nos=[],
            )
        else:
            payload["package_split_status"] = "manual_pending"
            payload["package_split_error"] = plan.manual_reason
        return payload

    if not plan.required:
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
        )
        return payload

    if not allow_page_write:
        payload["package_split_status"] = "write_disabled"
        payload["package_split_error"] = "页面拆包写入已关闭，本次只生成拆包计划。"
        return payload
    if not await confirm_tent_package_split_plan_in_cmd(plan):
        payload["package_split_status"] = "user_cancelled"
        payload["package_split_error"] = "用户取消拆分包裹。"
        return payload

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

    result = await execute_tent_package_split(page, plan)
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
        )
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
SAFE_RETRY_COMPLETION_PHRASE = "文件夹和定制文件已完成校验；本次为安全/预览运行，不写入最终完成列表。"
SAFE_RETRY_TENT_SKU_COMPLETION_PHRASE = "联系方式、文件夹、定制文件和帐篷 SKU 均已完成校验；本次为安全/预览运行，不写入最终完成列表。"
SAFE_RETRY_TENT_PACKAGE_SPLIT_COMPLETION_PHRASE = "联系方式、文件夹、定制文件、帐篷 SKU 和拆分包裹均已完成校验；本次为安全/预览运行，不写入最终完成列表。"


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
        message.replace(FORMAL_TENT_PACKAGE_SPLIT_COMPLETION_PHRASE, SAFE_RETRY_TENT_PACKAGE_SPLIT_COMPLETION_PHRASE)
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


async def process_batch_order_item(
    page,
    item: BatchOrderItem,
    amazon_quantity_client: AmazonOrderQuantityClient,
    dedupe_path: str | Path | None = None,
    payment_window_hours: float = 24,
    search_timeout_sec: int = 20,
    folder_root: str | Path | None = None,
    folder_date: str | None = None,
    create_folder: bool = True,
    download_custom_zip: bool = True,
    allow_sku_adjustment_page_write: bool | None = None,
    allow_package_split_page_write: bool | None = None,
    ignore_dedupe: bool = False,
    write_dedupe: bool = True,
) -> dict[str, Any]:
    """处理单个批量订单候选项，串联联系方式、文件夹和 SKU 调整流程。"""
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

    await close_order_detail_dialog(page)
    search_meta = await fill_order_search(page, item.platform_order_no, "platform")
    system_order_nos = await wait_for_orders_in_list(page, item.platform_order_no, "platform", search_timeout_sec)
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
        "source_page": item.source_page,
        "source_scroll_top": item.source_scroll_top,
        "dedupe_read_enabled": dedupe_read_enabled,
        "dedupe_write_enabled": write_dedupe,
    }
    unique_system_order_nos = list(dict.fromkeys(system_order_nos))
    if item.system_order_no and item.system_order_no not in unique_system_order_nos:
        payload["status"] = "search_context_mismatch"
        payload["message"] = (
            f"平台单号搜索结果不包含列表中的系统单号 {item.system_order_no}；"
            f"实际结果：{unique_system_order_nos}。为避免写错订单已停止。"
        )
        await close_order_detail_dialog(page)
        return payload
    if len(unique_system_order_nos) != 1:
        payload["status"] = "split_order_after_search"
        payload["message"] = f"平台单号 {item.platform_order_no} 匹配到 {len(unique_system_order_nos)} 个系统单号，按拆分订单跳过。"
        payload["system_order_nos"] = unique_system_order_nos
        await close_order_detail_dialog(page)
        return payload
    if not product_match:
        payload["status"] = "not_tent"
        payload["message"] = "订单 ASIN/SKU 不在当前支持的定制品类中，已跳过。"
        await close_order_detail_dialog(page)
        return payload

    item.asin = product_match.asin
    item.parent_asin = product_match.parent_asin
    item.product_type = product_match.product_type
    item.paid_at_text = paid_at_text

    if payment_status != "recent":
        payload["status"] = "payment_time_unknown" if payment_status == "unknown" else "payment_window_expired"
        payload["message"] = (
            "未能从订单列表识别付款时间，已跳过。"
            if payment_status == "unknown"
            else f"付款时间不在最近 {payment_window_hours:g} 小时内，已跳过。"
        )
        await close_order_detail_dialog(page)
        return payload

    system_order_no = unique_system_order_nos[0]
    await close_order_detail_dialog(page)
    await click_system_order(page, system_order_no)
    await wait_for_detail(page, system_order_no)
    await assert_current_detail_order(page, system_order_no, item.platform_order_no, "before extraction")
    folder_context = await collect_order_folder_json_context(
        page,
        item,
        amazon_quantity_client,
        system_order_no,
        staging_root=Path("logs") / "custom_zip_staging",
        download_custom_zip=download_custom_zip,
    )
    shipping_address_text = await read_detail_shipping_address_text(page)
    payload["shipping_address_text"] = _short_text(shipping_address_text, 1000)
    await assert_current_detail_order(page, system_order_no, item.platform_order_no, "before writeback")
    quantity_result = folder_context.get("amazon_quantity_result")
    if isinstance(quantity_result, AmazonOrderQuantityResult):
        payload.update(quantity_result.to_log_dict())
    zip_bundle = folder_context.get("zip_bundle")
    if zip_bundle is not None:
        payload.update(zip_bundle.to_log_dict())
    payload["recipient_name"] = folder_context.get("recipient_name")
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
    sku_adjustment_required = order_requires_tent_sku_adjustment(item, order_lines_for_sku)
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
    payload["sku_adjustment_required"] = sku_adjustment_required
    payload["folder_already_complete"] = folder_already_complete
    payload["sku_adjustment_already_done"] = sku_adjustment_already_done
    payload["package_split_already_done"] = package_split_already_done
    sku_adjustment_page_write_enabled = (
        create_folder if allow_sku_adjustment_page_write is None else bool(allow_sku_adjustment_page_write)
    )
    package_split_page_write_enabled = (
        create_folder if allow_package_split_page_write is None else bool(allow_package_split_page_write)
    )
    payload["sku_adjustment_page_write_enabled"] = sku_adjustment_page_write_enabled
    payload["package_split_page_write_enabled"] = package_split_page_write_enabled
    if (zip_bundle is None or getattr(zip_bundle, "status", "ok") != "ok") or (
        isinstance(quantity_result, AmazonOrderQuantityResult) and quantity_result.status != AMAZON_QUANTITY_RESOLVED
    ) or folder_context.get("order_line_error"):
        folder_result = json_context_failure_folder_result(folder_context, folder_root=folder_root or DEFAULT_FOLDER_ROOT)
        payload.update(folder_result.to_log_dict())
        payload["status"] = "updated_folder_failed"
        payload["message"] = f"定制 zip / JSON / Amazon 数量信息未准备好，未加入最终完成列表：{format_folder_failure_reason(folder_result)}"
        await close_order_detail_dialog(page)
        return payload

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
    payload["contact_writeback_already_done"] = contact_writeback_already_done

    skip_contact_writeback = False
    contact_stage_status = "written"
    if contact_writeback_already_done:
        selected_contact = ContactInfo(phone=None, email=None, source_count=0, source_excerpt="contact writeback already completed")
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
        selected_contact = await choose_contact_candidate_in_cmd(item.platform_order_no, system_order_no, contact_candidates)

    if selected_contact is None:
        payload["status"] = "contact_choice_skipped" if contact_candidates else "missing_contact"
        if not contact_candidates:
            payload["detail_text_preview"] = build_detail_text_preview(texts)
            payload["message"] = "定制化 JSON 中未解析到电话/邮箱，需要人工检查。"
        else:
            payload["message"] = "已识别联系方式候选，但用户取消写回。"
        await close_order_detail_dialog(page)
        return payload

    payload["phone"] = selected_contact.phone
    payload["email"] = selected_contact.email
    payload["writeback_fields"] = contact_writeback_fields(selected_contact)
    payload["missing_contact_fields"] = missing_contact_fields(selected_contact)
    payload["source_excerpt"] = selected_contact.source_excerpt
    if skip_contact_writeback:
        saved = True
        message = (
            "联系方式此前已完成，本轮跳过写回。"
            if contact_writeback_already_done
            else "定制化 JSON 中没有电话/邮箱，本次不写回联系方式。"
        )
    else:
        saved, message = await update_current_detail_contact(
            page,
            selected_contact,
            expected_system_order_no=system_order_no,
            expected_platform_order_no=item.platform_order_no,
            source_system_order_no=system_order_no,
            confirm_callback=confirm_writeback_in_cmd,
        )
    payload["update_messages"] = [f"{system_order_no}: {message}"]
    if saved:
        payload["source_system_order_no"] = system_order_no
        payload["updated_system_order_nos"] = [system_order_no]
        contact_recorded = False
        if not contact_writeback_already_done:
            contact_recorded = record_contact_writeback_if_allowed(
                dedupe_path,
                item.platform_order_no,
                system_order_no,
                contact_status=contact_stage_status,
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
            if await confirm_folder_creation_in_cmd(item.platform_order_no, system_order_no, folder_result):
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
                        shipping_address_text=shipping_address_text,
                        dedupe_path=dedupe_path,
                        write_dedupe=write_dedupe and create_folder,
                        allow_page_write=sku_adjustment_page_write_enabled,
                        read_dedupe=dedupe_read_enabled,
                    )
                    payload.update(sku_payload)
                    if payload.get("sku_adjustment_complete"):
                        package_payload = await run_tent_package_split_stage(
                            page,
                            item,
                            system_order_no,
                            folder_result,
                            shipping_address_text=shipping_address_text,
                            dedupe_path=dedupe_path,
                            write_dedupe=write_dedupe and create_folder,
                            allow_page_write=package_split_page_write_enabled,
                            read_dedupe=dedupe_read_enabled,
                        )
                        payload.update(package_payload)
                        if payload.get("package_split_complete"):
                            payload["status"] = "updated"
                            payload["message"] = "联系方式、文件夹、定制文件、帐篷 SKU 和拆分包裹均已完成，已加入最终完成列表。"
                        else:
                            payload["status"] = "updated_folder_created_package_split_failed"
                            payload["message"] = (
                                "联系方式、文件夹、定制文件和帐篷 SKU 已完成，但拆分包裹未完成，已保留后续拆包："
                                f"{payload.get('package_split_error') or payload.get('package_split_status') or '-'}"
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
                            "联系方式和文件夹已完成，但帐篷 SKU 未完成，已保留后续补 SKU："
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
                    f"{message if skip_contact_writeback else build_writeback_without_processed_message(selected_contact)} "
                    f"文件夹已创建，但定制 zip 未完成：{finalize_result.get('custom_zip_status')}"
                )
        else:
            payload["status"] = "updated_folder_failed"
            prefix_message = message if skip_contact_writeback else build_writeback_without_processed_message(selected_contact)
            payload["message"] = f"{prefix_message} 文件夹生成失败：{format_folder_failure_reason(folder_result)}"
    else:
        payload["status"] = "needs_manual_save"
        payload["updated_system_order_nos"] = []
        payload["message"] = f"联系方式未保存，未加入联系方式完成列表：{message}"
    await close_order_detail_dialog(page)
    return payload

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
    """生成写回成功但未写入最终完成状态的提示消息。"""
    fields = contact_writeback_fields(contact)
    missing_fields = missing_contact_fields(contact)
    field_text = "、".join(fields) if fields else "无字段"
    suffix = f"；缺少 {'、'.join(missing_fields)}" if missing_fields else ""
    return f"已写回：{field_text}{suffix}，已加入联系方式完成列表；文件夹或定制 zip 未完成，未加入最终完成列表，后续会直接补建文件夹。"


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
        "orders_to_update": orders_to_update,
    }


def _compact_log_mapping(source: Mapping[str, Any], keys: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """按白名单复制日志字段，跳过空值。"""

    result: dict[str, Any] = {}
    for key in keys:
        if key not in source:
            continue
        value = source.get(key)
        if value in (None, "", [], {}):
            continue
        result[key] = copy.deepcopy(value)
    return result


def _compact_text_value(value: Any, limit: int = 240) -> str:
    """日志用短文本，避免完整表格行和长消息撑大 JSON。"""

    return _short_text(value, limit)


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
            "page_size_1000",
            "wait_for_visible_rows",
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
    "phone",
    "email",
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
    "sku_adjustment_already_done",
    "sku_adjustment_page_write_enabled",
    "sku_adjustment_status",
    "sku_adjustment_complete",
    "sku_adjustment_error",
    "sku_adjustment_recorded",
    "package_split_required",
    "package_split_already_done",
    "package_split_page_write_enabled",
    "package_split_status",
    "package_split_complete",
    "package_split_error",
    "package_split_recorded",
    "package_split_system_order_nos",
    "screenshot_file",
)


_FAILURE_STATUSES: set[str] = {
    "error",
    "updated_folder_failed",
    "updated_folder_created_zip_failed",
    "updated_folder_created_package_split_failed",
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
        compact = _compact_log_mapping(contact, ("system_order_no", "phone", "email", "missing_fields"))
        if contact.get("source_excerpt"):
            compact["source_excerpt"] = _compact_text_value(contact.get("source_excerpt"), 160)
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
    if "source_excerpt" in compact:
        compact["source_excerpt"] = _compact_text_value(compact["source_excerpt"], 180)
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
        if item.get("shipping_address_text"):
            compact["shipping_address_text_preview"] = _compact_text_value(item.get("shipping_address_text"), 240)
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
) -> list[BatchOrderItem]:
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
    await ensure_page_size_1000(page, debug)
    await ensure_batch_key_columns_visible(page, debug)
    search_meta = await fill_order_search(page, platform_order_no, "platform")
    debug["search_meta"] = search_meta
    if not search_meta.get("search_validation_ok"):
        raise RuntimeError(str(search_meta.get("search_validation_message") or "平台单号搜索框校验失败。"))

    system_order_nos = await wait_for_orders_in_list(page, platform_order_no, "platform", args.search_timeout_sec)
    debug["system_order_nos_after_search"] = system_order_nos
    wait_result = await wait_for_visible_batch_order_rows(page, debug)
    debug["detected_headers"] = wait_result.get("headers") or []
    debug["column_indexes"] = wait_result.get("column_indexes") or {}
    rows = await collect_visible_batch_order_rows(page, 1, 0)
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
    )
    debug["scan_finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
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
    return candidates


async def process_batch_candidate_with_policy(
    page,
    item: BatchOrderItem,
    amazon_quantity_client: AmazonOrderQuantityClient,
    args: argparse.Namespace,
    processed: set[str],
    *,
    ignore_dedupe: bool = False,
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
        write_dedupe=dedupe_write_enabled,
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
    elif args.no_create_folder or not dedupe_write_enabled:
        item_result["dedupe_write_skipped"] = True
    return item_result, True


async def run_batch_round(page, args: argparse.Namespace, log_dir: Path) -> dict[str, Any]:
    """执行一轮批量巡检，筛选候选订单并逐单处理。"""
    dedupe_write_enabled = _dedupe_write_enabled(args)
    if dedupe_write_enabled:
        migrate_dedupe_file(args.dedupe_path)
    processed = load_processed_platform_orders(args.dedupe_path)
    amazon_quantity_client = AmazonOrderQuantityClient.from_env(args.env_path)
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
    amazon_quantity_client = AmazonOrderQuantityClient.from_env(args.env_path)
    await close_order_detail_dialog(page)
    await ensure_order_view_mode(page, debug_dir=getattr(args, "debug_log_dir", "debug/logs"))
    candidate_debug: dict[str, Any] = {}
    try:
        candidates = await collect_retry_order_candidates(page, args, processed, candidate_debug)
        scan_log_file = write_batch_scan_log(log_dir, candidate_debug)
    except Exception as exc:
        screenshot_file = None
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
        payload["status"] = "retry_no_candidate"
        payload["message"] = "已按平台单号搜索，但没有从批量表格行构造出可重测候选。"
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
                ignore_dedupe=True,
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
            try:
                await close_order_detail_dialog(page)
            except Exception:
                pass

    payload["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_batch_result(log_dir, payload)
    return payload


async def run_retry_order(args: argparse.Namespace) -> dict[str, Any]:
    """安全重测入口：沿用批量巡检登录、订单视图和单项处理流程。"""

    log_dir = Path(args.log_dir).resolve()
    login_config = LoginConfig()
    if not args.no_auto_login:
        login_config = load_login_config(args.env_path)
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
        login_config = load_login_config(args.env_path)
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
        login_config = load_login_config(args.env_path)
    amazon_quantity_client = AmazonOrderQuantityClient.from_env(args.env_path)
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
                )
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
                    f"已从系统单号 {source_system_order_no} 获取联系方式；"
                    f"写回 {len(updated)}/{len(system_order_nos)} 个系统单号。请检查失败项。"
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
                result.message = f"{result.message} 文件夹生成失败：{format_folder_failure_reason(folder_result)}。"
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
                    result.message = f"{result.message} 定制 zip 未完成：{finalize_result.get('custom_zip_status')}。"
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

