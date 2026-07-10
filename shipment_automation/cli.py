from __future__ import annotations

import argparse
import asyncio
import json

from .erp_mark_ship import run_erp_mark_worker
from .lingxing_source import run_shipment_scan
from .logistics_worker import run_logistics_worker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="自动标发候选扫描与处理工具。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_scan_parser(subparsers)
    add_logistics_parser(subparsers)
    add_erp_mark_parser(subparsers)
    return parser


def add_scan_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("scan", description="从领星 ERP 扫描自动标发候选订单。")
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="安全扫描模式；仍写入本地队列，但不查阿里、不回填 ERP、不发邮件。",
    )
    parser.add_argument(
        "--execute",
        dest="dry_run",
        action="store_false",
        help="预留显式执行开关；第一阶段仍只执行候选扫描和入队。",
    )
    parser.add_argument("--shipment-tag", default=None, help="覆盖配置中的专属发货标签。")
    parser.add_argument("--queue-path", default="data/shipment_queue.sqlite3", help="SQLite 自动标发队列路径。")
    parser.add_argument("--scan-limit", type=int, default=0, help="最多扫描多少条订单行；0 表示不限制。")
    parser.add_argument("--profile-dir", default="browser_profile", help="保存领星登录状态的浏览器配置目录。")
    parser.add_argument("--env-path", default=".env", help="保存领星账号密码的 .env 文件路径。")
    parser.add_argument("--no-auto-login", action="store_true", help="不读取 .env 自动登录，改为手动登录等待。")
    parser.add_argument("--log-dir", default="logs", help="保存扫描 JSON 日志的目录。")
    parser.add_argument("--debug-log-dir", default="debug/logs", help="保存页面诊断文件的目录。")
    parser.add_argument("--browser-channel", default="chrome", help="默认使用系统 Chrome；可填 msedge 或 bundled。")
    parser.add_argument("--headless", action="store_true", help="无头模式。首次登录不要使用。")
    parser.add_argument("--keep-browser-open", action="store_true", help="脚本结束后不自动关闭浏览器。")
    parser.add_argument("--login-timeout-sec", type=int, default=300, help="等待手动登录的最长秒数。")
    parser.add_argument("--width", type=int, default=1920, help="无头模式固定浏览器视口宽度。")
    parser.add_argument("--height", type=int, default=1080, help="无头模式固定浏览器视口高度。")
    parser.add_argument("--verbose", action="store_true", help="显示重复跳过等调试明细。")
    parser.add_argument("--json", action="store_true", help="只输出 JSON，方便其它程序读取。")
    return parser


def add_logistics_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("logistics", description="查询阿里国际站物流详情并生成待标发列表。")
    parser.add_argument("--from-queue", action="store_true", help="从 SQLite 队列读取 NEW / NOT_READY / ERROR 记录。")
    parser.add_argument("--limit", type=int, default=20, help="最多查询多少条物流记录；0 表示不限制。")
    parser.add_argument("--dry-run", action="store_true", default=True, help="只查询和输出，不更新本地队列。")
    parser.add_argument("--update-queue", action="store_true", help="显式更新本地 SQLite 队列状态和物流字段。")
    parser.add_argument("--watch", action="store_true", help="按间隔循环巡检。")
    parser.add_argument("--interval-hours", type=float, default=24, help="watch 模式巡检间隔小时数，默认 24。")
    parser.add_argument("--queue-path", default="data/shipment_queue.sqlite3", help="SQLite 自动标发队列路径。")
    parser.add_argument("--profile-dir", default="browser_profile", help="保存浏览器登录状态的配置目录。")
    parser.add_argument("--env-path", default=".env", help="保存阿里国际站账号密码的 .env 文件路径。")
    parser.add_argument("--no-auto-login", action="store_true", help="不读取 .env 自动登录阿里，改为手动登录等待。")
    parser.add_argument("--login-timeout-sec", type=int, default=300, help="等待阿里登录或验证完成的最长秒数。")
    parser.add_argument("--browser-channel", default="chrome", help="默认使用系统 Chrome；可填 msedge 或 bundled。")
    parser.add_argument("--headless", action="store_true", help="无头模式。首次登录阿里不建议使用。")
    parser.add_argument("--keep-browser-open", action="store_true", help="脚本结束后不自动关闭浏览器。")
    parser.add_argument("--width", type=int, default=1920, help="无头模式固定浏览器视口宽度。")
    parser.add_argument("--height", type=int, default=1080, help="无头模式固定浏览器视口高度。")
    parser.add_argument("--json", action="store_true", help="只输出 JSON，方便其它程序读取。")
    return parser


def add_erp_mark_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("erp-mark", description="从待标发队列执行 ERP 标发和出库。")
    parser.add_argument("--limit", type=int, default=20, help="最多处理多少条 READY_TO_MARK 记录；0 表示不限制。")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="只输出将处理的订单，不点击 ERP。")
    parser.add_argument("--execute", dest="dry_run", action="store_false", help="真实点击 ERP，并在用户确认后完成出库。")
    parser.add_argument("--queue-path", default="data/shipment_queue.sqlite3", help="SQLite 自动标发队列路径。")
    parser.add_argument("--profile-dir", default="browser_profile", help="保存领星登录状态的浏览器配置目录。")
    parser.add_argument("--env-path", default=".env", help="保存领星账号密码的 .env 文件路径。")
    parser.add_argument("--no-auto-login", action="store_true", help="不读取 .env 自动登录领星，改为手动登录等待。")
    parser.add_argument("--login-timeout-sec", type=int, default=300, help="等待领星登录完成的最长秒数。")
    parser.add_argument("--browser-channel", default="chrome", help="默认使用系统 Chrome；可填 msedge 或 bundled。")
    parser.add_argument("--headless", action="store_true", help="无头模式。真实 ERP 标发不建议使用。")
    parser.add_argument("--keep-browser-open", action="store_true", help="脚本结束后不自动关闭浏览器。")
    parser.add_argument("--debug-log-dir", default="debug/logs", help="保存页面诊断文件的目录。")
    parser.add_argument("--width", type=int, default=1920, help="无头模式固定浏览器视口宽度。")
    parser.add_argument("--height", type=int, default=1080, help="无头模式固定浏览器视口高度。")
    parser.add_argument("--json", action="store_true", help="只输出 JSON，方便其它程序读取。")
    return parser


def print_shipment_scan_result(payload: dict, *, verbose: bool = False) -> None:
    """输出自动标发候选扫描报告。"""

    print("\n自动标发候选扫描结果")
    print(f"状态：{payload.get('status') or '-'}")
    if payload.get("message"):
        print(f"说明：{payload.get('message')}")
    print(f"专属发货标签：{payload.get('shipment_tag_name') or '-'}")
    print(f"队列文件：{payload.get('queue_path') or '-'}")
    print(f"扫描总行数：{payload.get('scanned_row_count', 0)}")
    print(f"带专属发货标签行数：{payload.get('tagged_row_count', 0)}")
    print(f"带有效物流单号行数：{payload.get('valid_als_row_count', 0)}")
    print(f"入队帐篷行数：{payload.get('enqueued_count', 0)}")
    if verbose:
        print(f"重复跳过数量：{payload.get('duplicate_skipped_count', 0)}")
    print(f"MANUAL_REVIEW 数量：{payload.get('manual_review_count', 0)}")
    if payload.get("scan_log_file"):
        print(f"扫描日志：{payload.get('scan_log_file')}")

    enqueued = payload.get("enqueued_candidates") or []
    if enqueued:
        print("\n入队候选：")
        print("系统单号 | 平台单号 | 物流单号 | SKU | 标签 | 队列状态 | 备注")
        for item in enqueued:
            notes = "；".join(item.get("warnings") or []) or "-"
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('als_no') or '-'} | "
                f"{item.get('sku_text') or '-'} | "
                f"{item.get('tag_text') or '-'} | "
                f"{item.get('queue_status') or '-'} | "
                f"{notes}"
            )

    duplicates = payload.get("duplicate_skipped") or []
    attention_duplicates = [
        item
        for item in duplicates
        if item.get("existing_queue_status") in {"READY_TO_MARK", "MANUAL_REVIEW", "ERROR"}
    ]
    if attention_duplicates:
        print("\n队列已有待处理/异常记录：")
        print("系统单号 | 平台单号 | 物流单号 | 队列状态 | 原因")
        for item in attention_duplicates:
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('als_no') or '-'} | "
                f"{item.get('existing_queue_status') or '-'} | "
                f"{item.get('existing_last_error') or '-'}"
            )
    if verbose and duplicates:
        print("\n重复跳过：")
        for item in duplicates:
            existing = ""
            if item.get("existing_system_order_no") or item.get("existing_platform_order_no"):
                existing = (
                    f"；队列已存在系统单号：{item.get('existing_system_order_no') or '-'}"
                    f"；平台单号：{item.get('existing_platform_order_no') or '-'}"
                )
            print(
                f"- 系统单号：{item.get('system_order_no') or '-'}；"
                f"平台单号：{item.get('platform_order_no') or '-'}；"
                f"物流单号：{item.get('als_no') or '-'}；"
                f"队列状态：{item.get('existing_queue_status') or '-'}；"
                f"原因：{item.get('existing_last_error') or '-'}{existing}"
            )

    reviews = payload.get("manual_reviews") or []
    if reviews:
        print("\n需要复核：")
        for item in reviews:
            als_numbers = ", ".join(item.get("als_numbers") or [])
            print(
                f"- 系统单号：{item.get('system_order_no') or '-'}；"
                f"平台单号：{item.get('platform_order_no') or '-'}；"
                f"首选物流单号：{item.get('selected_als_no') or '-'}；"
                f"其它/相关物流单号：{als_numbers or '-'}；"
                f"原因：{item.get('message') or item.get('reason') or '-'}"
            )


def print_logistics_worker_result(payload: dict) -> None:
    """输出阿里物流查询和待标发报告。"""

    print("\n本次物流查询结果")
    print("系统单号 | 平台单号 | 物流单号 | 订单状态 | 队列状态 | 备注")
    query_results = payload.get("query_results") or []
    if query_results:
        for item in query_results:
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('als_no') or '-'} | "
                f"{item.get('status_text') or '-'} | "
                f"{item.get('queue_status') or '-'} | "
                f"{item.get('last_error') or '-'}"
            )
    else:
        print("- | - | - | - | - | -")

    warnings = payload.get("warnings") or []
    if warnings:
        print("\n提示")
        for warning in warnings:
            print(f"- {warning}")

    print("\n待标发列表")
    print("系统单号 | 平台单号 | 物流单号 | 国际物流服务商 | 国际物流单号 | 费用金额 | 计费重KG")
    ready_items = payload.get("ready_to_mark_items") or []
    if ready_items:
        for item in ready_items:
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('als_no') or '-'} | "
                f"{item.get('carrier') or '-'} | "
                f"{item.get('international_tracking_no') or '-'} | "
                f"{item.get('actual_total') or '-'} | "
                f"{item.get('chargeable_weight_kg') or '-'}"
            )
    else:
        print("- | - | - | - | - | - | -")

    skipped = payload.get("skipped_query_records") or []
    if skipped:
        print("\n本轮未查询的队列记录")
        print("系统单号 | 平台单号 | 物流单号 | 队列状态 | 原因")
        for item in skipped:
            reason = item.get("last_error") or _skipped_logistics_reason(item.get("queue_status"))
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('als_no') or '-'} | "
                f"{item.get('queue_status') or '-'} | "
                f"{reason or '-'}"
            )

    print("\n汇总信息")
    print(f"扫描页面数：{payload.get('scanned_page_count', 0)}")
    print(f"成功解析数：{payload.get('parsed_count', 0)}")
    print(f"READY_TO_MARK 数量：{payload.get('ready_to_mark_count', 0)}")
    print(f"NOT_READY 数量：{payload.get('not_ready_count', 0)}")
    print(f"MANUAL_REVIEW 数量：{payload.get('manual_review_count', 0)}")
    print(f"ERROR 数量：{payload.get('error_count', 0)}")


def _skipped_logistics_reason(queue_status: str | None) -> str:
    if queue_status == "READY_TO_MARK":
        return "已是 READY_TO_MARK，等待 ERP 标发。"
    if queue_status == "MANUAL_REVIEW":
        return "需要人工复核，不会自动重新查询物流。"
    if queue_status == "ERROR":
        return "上一阶段失败；本轮未进入查询范围，请查看错误后处理或重试。"
    return ""


def print_erp_mark_result(payload: dict) -> None:
    """输出 ERP 标发执行报告。"""

    print("\nERP 自动标发结果")
    print(f"状态：{payload.get('status') or '-'}")
    if payload.get("message"):
        print(f"说明：{payload.get('message')}")
    print(f"队列文件：{payload.get('queue_path') or '-'}")
    print(f"dry-run：{payload.get('dry_run')}")
    print("\n处理结果")
    print("系统单号 | 平台单号 | 物流单号 | ERP步骤 | 队列状态 | 备注")
    results = payload.get("results") or []
    if results:
        for item in results:
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('als_no') or '-'} | "
                f"{item.get('erp_step') or '-'} | "
                f"{item.get('queue_status') or '-'} | "
                f"{item.get('last_error') or '-'}"
            )
    else:
        print("- | - | - | - | - | -")
    print("\n汇总信息")
    print(f"待处理数量：{payload.get('total_count', 0)}")
    print(f"ERP_MARKED 数量：{payload.get('marked_count', 0)}")
    print(f"MANUAL_REVIEW 数量：{payload.get('manual_review_count', 0)}")
    print(f"ERROR 数量：{payload.get('error_count', 0)}")


async def run_logistics_cli(args: argparse.Namespace) -> int:
    while True:
        payload = await run_logistics_worker(args)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_logistics_worker_result(payload)
        if not args.watch:
            return 0 if payload.get("status") == "completed" else 1
        interval_seconds = max(float(args.interval_hours or 24), 0.01) * 3600
        await asyncio.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        payload = asyncio.run(run_shipment_scan(args))
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_shipment_scan_result(payload, verbose=bool(getattr(args, "verbose", False)))
        return 0 if payload.get("status") == "completed" else 1
    if args.command == "logistics":
        try:
            return asyncio.run(run_logistics_cli(args))
        except KeyboardInterrupt:
            return 130
    if args.command == "erp-mark":
        try:
            payload = asyncio.run(run_erp_mark_worker(args))
        except KeyboardInterrupt:
            return 130
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_erp_mark_result(payload)
        return 0 if payload.get("status") == "completed" else 1
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
