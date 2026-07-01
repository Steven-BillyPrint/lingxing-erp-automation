from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from .constants import DEFAULT_BATCH_INTERVAL_MINUTES
from .flows.contact_sync import run_batch, run_once, run_retry_order
from .models import SyncResult, format_rule_missing_lines
from .services.folder_builder import DEFAULT_FOLDER_ROOT


def prepare_retry_order_args(args: argparse.Namespace) -> argparse.Namespace:
    """校验安全重测参数；真正执行会走批量巡检链路的 run_retry_order。"""

    retry_order = str(getattr(args, "retry_order", "") or "").strip()
    if not retry_order:
        return args
    if args.order_no and args.order_no != retry_order:
        raise ValueError("--retry-order 与位置参数 order_no 不一致，请只保留一个平台单号。")
    args.retry_order = retry_order
    args.order_no = None
    args.batch = False
    args.loop = False
    args.no_dedupe_write = True
    args.no_create_folder = True
    return args


def prompt_for_missing_args(args: argparse.Namespace) -> argparse.Namespace:
    """在命令行交互中补齐用户未传入的必要参数。"""
    if args.order_no or getattr(args, "retry_order", None) or args.all_visible or args.batch or not sys.stdin.isatty():
        return args

    print("请输入要处理的订单号。")
    print("可以输入平台单号，例如 111-6622902-4192214；也可以输入系统单号。")
    order_no = input("订单号（留空则处理当前页面第一条系统单号）：").strip()
    args.order_no = order_no or None
    if args.apply is None:
        answer = input("是否真正写回页面？输入 y 写回，直接回车只预览：").strip().lower()
        args.apply = answer in {"y", "yes", "1", "是"}
    return args

def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器，定义自动化脚本支持的运行选项。"""
    parser = argparse.ArgumentParser(description="领星 ERP 订单批量巡检与安全重测工具。")
    parser.add_argument("order_no", nargs="?", help="平台单号或系统单号；留空时处理当前页面第一条系统单号。")
    parser.add_argument("--search-kind", choices=["platform", "system"], help="指定订单号类型。默认自动判断。")
    parser.add_argument("--retry-order", help="安全重测指定平台单号：走批量巡检单项流程，默认不写查重、不创建文件夹。")
    parser.add_argument("--no-dedupe-write", action="store_true", help="本次运行不写入 data/processed_platform_orders.json。")
    parser.add_argument("--apply", action="store_true", default=None, help="真正回填页面；不加则只预览解析结果。")
    parser.add_argument("--batch", action="store_true", help="批量巡检当前订单列表：只处理非拆分、含帐篷 ASIN 且未查重的平台单号。")
    parser.add_argument("--loop", action="store_true", help="批量巡检后持续循环运行。通常配合 --batch 使用。")
    parser.add_argument(
        "--batch-interval-hours",
        type=float,
        default=DEFAULT_BATCH_INTERVAL_MINUTES / 60,
        help="批量循环间隔小时数，兼容旧参数；默认 5 分钟。",
    )
    parser.add_argument(
        "--batch-interval-minutes",
        type=float,
        default=None,
        help="批量循环间隔分钟数；设置后优先于 --batch-interval-hours。",
    )
    parser.add_argument("--batch-payment-hours", type=float, default=96.0, help="批量巡检只处理最近多少小时内付款的订单，默认 24 小时。")
    parser.add_argument("--dedupe-path", default="data/processed_platform_orders.json", help="最终完成订单的查重状态文件。")
    parser.add_argument("--folder-root", default=DEFAULT_FOLDER_ROOT, help="订单定制文件夹根目录。")
    parser.add_argument("--folder-date", default=None, help="人工覆盖文件夹日期，格式 YYYY-MM-DD；仅用于补单或调试。")
    parser.add_argument("--no-create-folder", action="store_true", help="只预览/记录文件夹名，不实际创建订单定制文件夹。")
    parser.add_argument(
        "--allow-sku-adjustment",
        action="store_true",
        help="安全重测/预览模式下仍允许真实执行帐篷 SKU 页面调整；不影响查重写入或文件夹创建。",
    )
    parser.add_argument("--no-download-custom-zip", action="store_true", help="生成订单文件夹后不下载定制化图片 zip。")
    parser.add_argument("--debug-log-dir", default="debug/logs", help="进入订单管理页失败时保存截图、HTML 和诊断 JSON 的目录。")
    parser.add_argument("--batch-limit", type=int, default=0, help="每轮最多处理多少个候选订单；0 表示不限制。")
    parser.add_argument("--all-visible", action="store_true", help="保留参数：当前版本先处理当前页面第一条。")
    parser.add_argument("--profile-dir", default="browser_profile", help="保存领星登录状态的浏览器配置目录。")
    parser.add_argument("--env-path", default=".env", help="保存领星账号密码的 .env 文件路径。")
    parser.add_argument("--no-auto-login", action="store_true", help="不读取 .env 自动登录，改为手动登录等待。")
    parser.add_argument("--log-dir", default="logs", help="保存结果 JSON 和错误截图的目录。")
    parser.add_argument("--search-timeout-sec", type=int, default=20, help="搜索后等待目标订单出现在列表中的最长秒数。")
    parser.add_argument("--browser-channel", default="chrome", help="默认使用系统 Chrome；可填 msedge 或 bundled。")
    parser.add_argument("--headless", action="store_true", help="无头模式。首次登录不要使用。")
    parser.add_argument("--keep-browser-open", action="store_true", help="脚本结束后不自动关闭浏览器。")
    parser.add_argument("--login-timeout-sec", type=int, default=300, help="等待手动登录的最长秒数。")
    parser.add_argument("--width", type=int, default=1920, help="无头模式固定浏览器视口宽度。")
    parser.add_argument("--height", type=int, default=1080, help="无头模式固定浏览器视口高度；有界面模式使用真实窗口高度。")
    parser.add_argument("--json", action="store_true", help="只输出 JSON，方便其它程序读取。")
    return parser

def print_result(result: SyncResult) -> None:
    """将单次同步结果输出到控制台，便于人工确认执行状态。"""
    print("\n处理结果")
    print(f"系统单号：{result.system_order_no or '-'}")
    if result.system_order_nos and len(result.system_order_nos) > 1:
        print(f"拆分系统单号：{', '.join(result.system_order_nos)}")
    if result.source_system_order_no:
        print(f"联系方式来源单号：{result.source_system_order_no}")
    if result.updated_system_order_nos:
        print(f"已写回单号：{', '.join(result.updated_system_order_nos)}")
    print(f"电话：{result.phone or '-'}")
    print(f"买家邮箱：{result.email or '-'}")
    print(f"状态：{result.status}")
    print(f"说明：{result.message}")
    if result.folder_status:
        print(f"文件夹状态：{result.folder_status}")
    if result.folder_preview and isinstance(result.folder_preview, Mapping):
        for line in format_rule_missing_lines(
            status=str(result.folder_preview.get("folder_status") or ""),
            title=result.folder_preview.get("folder_missing_rule_title"),
            value=result.folder_preview.get("folder_missing_rule_value"),
            customization_pairs=(
                result.folder_preview.get("customization_pairs")
                if isinstance(result.folder_preview.get("customization_pairs"), Mapping)
                else None
            ),
            missing_rule_line=result.folder_preview.get("folder_missing_rule_line"),
            error=result.folder_preview.get("folder_error"),
        ):
            print(line)
    if result.result_file:
        print(f"结果文件：{result.result_file}")
    if result.screenshot_file:
        print(f"截图：{result.screenshot_file}")
    if result.custom_zip_status:
        print(f"定制zip状态：{result.custom_zip_status}")
        if result.custom_zip_filename:
            print(f"定制zip文件：{result.custom_zip_filename}")
        if result.custom_zip_path:
            print(f"定制zip路径：{result.custom_zip_path}")

def main() -> int:
    """作为命令行入口，解析参数并调度对应的自动化流程。"""
    parser = build_parser()
    try:
        args = prepare_retry_order_args(parser.parse_args())
    except ValueError as exc:
        parser.error(str(exc))
    args = prompt_for_missing_args(args)
    if args.apply is None:
        args.apply = False

    if args.retry_order:
        args.apply = True
        payload = asyncio.run(run_retry_order(args))
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("\n安全重测结果")
            print(f"平台单号：{payload.get('retry_order') or '-'}")
            print(f"候选订单：{payload.get('candidate_count', 0)}")
            print(f"写回成功：{payload.get('updated_count', 0)}")
            print(f"跳过/失败：{payload.get('skipped_count', 0)}")
            if payload.get("result_file"):
                print(f"结果文件：{payload['result_file']}")
            print(f"查重写入：{'开启' if payload.get('dedupe_write_enabled') else '关闭'}")
        return 0 if payload.get("status") in {"completed", "retry_no_candidate"} else 1

    if args.batch:
        args.apply = True
        payload = asyncio.run(run_batch(args))
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("\n批量巡检结果")
            print(f"候选订单：{payload.get('candidate_count', 0)}")
            print(f"写回成功：{payload.get('updated_count', 0)}")
            print(f"跳过/失败：{payload.get('skipped_count', 0)}")
            if payload.get("result_file"):
                print(f"结果文件：{payload['result_file']}")
            print(f"查重列表：{Path(args.dedupe_path).resolve()}")
        return 0

    result = asyncio.run(run_once(args))
    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        print_result(result)
    return 0 if result.status in {"preview", "updated", "needs_manual_save"} else 1
