from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from .erp_mark_ship import run_erp_mark_worker
from .lingxing_source import run_shipment_scan
from .logistics_worker import run_logistics_worker
from .queue_manager import run_interactive_queue_manager
from .queue_store import ShipmentWorkflowStore


SCAN_INCOMPLETE_EXIT_CODE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="自动标发候选扫描与处理工具。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_scan_parser(subparsers)
    add_logistics_parser(subparsers)
    add_erp_mark_parser(subparsers)
    add_queue_parser(subparsers)
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


def add_queue_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("queue", description="查看和管理自动标发队列 V2。")
    actions = parser.add_subparsers(dest="queue_action", required=True)

    list_parser = actions.add_parser("list", help="列出队列任务。")
    list_parser.add_argument("--attention-only", action="store_true", help="只显示冲突、阻止和连续失败任务。")
    list_parser.add_argument("--limit", type=int, default=0)

    history_parser = actions.add_parser("history", help="查看单个物流单号的事件历史。")
    history_parser.add_argument("--logistics-no", required=True)

    retry_parser = actions.add_parser("retry", help="将被阻止或失败的阶段重新放回自动流程。")
    retry_parser.add_argument("--logistics-no", required=True)
    retry_parser.add_argument("--stage", choices=("logistics", "erp", "email"), required=True)
    retry_parser.add_argument("--reason", default="用户手动要求重试")
    retry_parser.add_argument("--execute", action="store_true")

    conflict_parser = actions.add_parser("resolve-conflict", help="确认物流单号应归属的 ERP 订单。")
    conflict_parser.add_argument("--logistics-no", required=True)
    conflict_parser.add_argument("--system-order-no", required=True)
    conflict_parser.add_argument("--platform-order-no", required=True)
    conflict_parser.add_argument("--execute", action="store_true")

    cancel_parser = actions.add_parser("cancel", help="取消自动标发任务并保留历史。")
    cancel_parser.add_argument("--logistics-no", required=True)
    cancel_parser.add_argument("--reason", required=True)
    cancel_parser.add_argument("--execute", action="store_true")

    manage_parser = actions.add_parser("manage", help="进入交互式队列管理。")
    manage_parser.add_argument("--queue-path", default="data/shipment_queue.sqlite3")

    for action_parser in (list_parser, history_parser, retry_parser, conflict_parser, cancel_parser):
        action_parser.add_argument("--queue-path", default="data/shipment_queue.sqlite3")
        action_parser.add_argument("--json", action="store_true")
    return parser


def print_shipment_scan_result(payload: dict, *, verbose: bool = False) -> None:
    """输出自动标发候选扫描报告。"""

    print("\n自动标发候选扫描结果")
    print(f"状态：{payload.get('status') or '-'}")
    if payload.get("message"):
        print(f"说明：{payload.get('message')}")
    print(f"专属发货标签：{payload.get('shipment_tag_name') or '-'}")
    print(f"队列文件：{payload.get('queue_path') or '-'}")
    table_total = payload.get("table_total_count")
    scanned_count = int(payload.get("scanned_row_count", 0) or 0)
    print(f"ERP 待审核总数：{table_total if table_total is not None else '-'}")
    print(f"成功读取行数：{scanned_count}")
    print(f"带专属发货标签行数：{payload.get('tagged_row_count', 0)}")
    print(f"带有效物流单号行数：{payload.get('valid_logistics_row_count', 0)}")
    print(f"本轮新增队列：{payload.get('enqueued_count', 0)}")
    print(f"已有队列刷新：{payload.get('refreshed_count', 0)}")
    immediate_count = int(payload.get("immediate_logistics_count", 0) or 0) + int(
        payload.get("immediate_erp_count", 0) or 0
    )
    if immediate_count:
        print(f"本轮重新命中候选：已安排立即查询/执行 {immediate_count} 条。")
    attention_count = int(payload.get("conflict_count", 0) or 0) + int(
        payload.get("manual_review_count", 0) or 0
    )
    print(f"冲突/需要复核数量：{attention_count}")
    if verbose:
        print(f"重复跳过数量：{payload.get('duplicate_skipped_count', 0)}")
    if not payload.get("scan_complete") and table_total is not None:
        missing_count = max(0, int(table_total) - scanned_count)
        print(
            f"警告：ERP 共 {table_total} 条，成功读取 {scanned_count} 条，缺少 {missing_count} 条；"
            "已禁止人工完成判定，将在下轮继续补扫。"
        )
        incomplete_fields = int(payload.get("incomplete_field_count", 0) or 0)
        if incomplete_fields:
            print(f"另有 {incomplete_fields} 条记录的关键列未完整读取。")
    if payload.get("scan_log_file"):
        print(f"扫描日志：{payload.get('scan_log_file')}")

    enqueued = payload.get("enqueued_candidates") or []
    if enqueued:
        print("\n入队候选：")
        print("系统单号 | 平台单号 | 物流单号 | 物流状态 | 备注")
        for item in enqueued:
            notes = "；".join(item.get("warnings") or []) or "-"
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('logistics_no') or '-'} | "
                "PENDING | "
                f"{notes}"
            )

    manual_completed = payload.get("manual_completed") or []
    if manual_completed:
        print(
            f"\n人工已完成：{len(manual_completed)} 条，"
            "已结案并停止后续处理。详情见扫描日志。"
        )
        if verbose:
            print("系统单号 | 平台单号 | 物流单号")
            for item in manual_completed:
                print(
                    f"{item.get('system_order_no') or '-'} | "
                    f"{item.get('platform_order_no') or '-'} | "
                    f"{item.get('logistics_no') or '-'}"
                )

    duplicates = payload.get("duplicate_skipped") or []
    attention_duplicates = [item for item in duplicates if item.get("conflict")]
    if attention_duplicates:
        print("\n物流单号归属冲突：")
        print("系统单号 | 平台单号 | 物流单号 | 已有系统单号 | 已有平台单号")
        for item in attention_duplicates:
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('logistics_no') or '-'} | "
                f"{item.get('existing_system_order_no') or '-'} | "
                f"{item.get('existing_platform_order_no') or '-'}"
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
                f"物流单号：{item.get('logistics_no') or '-'}；"
                f"身份状态：{item.get('existing_identity_state') or '-'}；"
                f"物流状态：{item.get('existing_logistics_state') or '-'}；"
                f"ERP状态：{item.get('existing_erp_state') or '-'}；"
                f"原因：{item.get('existing_last_error') or '-'}{existing}"
            )

    reviews = payload.get("manual_reviews") or []
    if reviews:
        print("\n需要复核：")
        for item in reviews:
            logistics_numbers = ", ".join(item.get("logistics_numbers") or [])
            print(
                f"- 系统单号：{item.get('system_order_no') or '-'}；"
                f"平台单号：{item.get('platform_order_no') or '-'}；"
                f"首选物流单号：{item.get('selected_logistics_no') or '-'}；"
                f"其它/相关物流单号：{logistics_numbers or '-'}；"
                f"原因：{item.get('message') or item.get('reason') or '-'}"
            )


def print_logistics_worker_result(payload: dict) -> None:
    """输出阿里物流查询和待标发报告。"""

    print("\n本次物流查询结果")
    print("系统单号 | 平台单号 | 物流单号 | 订单状态 | 物流状态 | 备注")
    query_results = payload.get("query_results") or []
    if query_results:
        for item in query_results:
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('logistics_no') or '-'} | "
                f"{item.get('status_text') or '-'} | "
                f"{item.get('logistics_state') or '-'} | "
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
                f"{item.get('logistics_no') or '-'} | "
                f"{item.get('carrier') or '-'} | "
                f"{item.get('international_tracking_no') or '-'} | "
                f"{item.get('actual_total') or '-'} | "
                f"{item.get('chargeable_weight_kg') or '-'}"
            )
    else:
        print("- | - | - | - | - | - | -")

    skipped = payload.get("skipped_query_records") or []
    if skipped:
        print("\n需关注的队列记录")
        print("系统单号 | 平台单号 | 物流单号 | 阶段状态 | 原因")
        for item in skipped:
            reason = item.get("last_error") or _skipped_logistics_reason(
                item.get("logistics_state"), item.get("erp_state")
            )
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('logistics_no') or '-'} | "
                f"{item.get('stage_state') or '-'} | "
                f"{reason or '-'}"
            )

    print("\n汇总信息")
    print(f"扫描页面数：{payload.get('scanned_page_count', 0)}")
    print(f"成功解析数：{payload.get('parsed_count', 0)}")
    print(f"READY 数量：{payload.get('ready_count', 0)}")
    print(f"WAITING 数量：{payload.get('waiting_count', 0)}")
    print(f"BLOCKED 数量：{payload.get('blocked_count', 0)}")
    print(f"RETRYABLE 数量：{payload.get('retryable_count', 0)}")


def _skipped_logistics_reason(logistics_state: str | None, erp_state: str | None) -> str:
    if erp_state == "DONE":
        return "ERP 已完成。"
    if logistics_state == "READY":
        return "物流已就绪，等待 ERP 标发。"
    if logistics_state == "BLOCKED" or erp_state == "BLOCKED":
        return "该阶段已阻止，需要显式人工放行。"
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
    print("系统单号 | 平台单号 | 物流单号 | ERP步骤 | ERP状态 | 检查点 | 备注")
    results = payload.get("results") or []
    if results:
        for item in results:
            print(
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('platform_order_no') or '-'} | "
                f"{item.get('logistics_no') or '-'} | "
                f"{item.get('erp_step') or '-'} | "
                f"{item.get('erp_state') or '-'} | "
                f"{item.get('erp_checkpoint') or '-'} | "
                f"{item.get('last_error') or '-'}"
            )
    else:
        print("- | - | - | - | - | - | -")
    reminders = payload.get("store_fulfillment_reminders") or []
    if reminders:
        print("\n店小秘待标发提示")
        print("独立站单号 | 系统单号 | 物流单号 | 国际物流服务商 | 国际物流单号 | 备注")
        for item in reminders:
            print(
                f"{item.get('independent_order_no') or '-'} | "
                f"{item.get('system_order_no') or '-'} | "
                f"{item.get('logistics_no') or '-'} | "
                f"{item.get('carrier') or '-'} | "
                f"{item.get('international_tracking_no') or '-'} | "
                f"{item.get('message') or '-'}"
            )
    print("\n汇总信息")
    print(f"候选数量：{payload.get('total_count', 0)}")
    print(f"完成数量：{payload.get('done_count', 0)}")
    print(f"跳过数量：{payload.get('skipped_count', 0)}")
    print(f"尾程单号阻止数量：{payload.get('tracking_blocked_count', 0)}")
    print(f"BLOCKED 数量：{payload.get('blocked_count', 0)}")
    print(f"RETRYABLE 数量：{payload.get('retryable_count', 0)}")


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


def run_queue_cli(args: argparse.Namespace) -> int:
    store = ShipmentWorkflowStore(args.queue_path)
    action = args.queue_action
    if action == "manage":
        return run_interactive_queue_manager(store)
    if action == "list":
        rows = store.list_attention(limit=args.limit) if args.attention_only else store.list_all_jobs(limit=args.limit)
        payload = {"status": "completed", "items": rows}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_queue_list(rows)
        return 0
    if action == "history":
        events = [asdict(event) for event in store.history(args.logistics_no)]
        payload = {"status": "completed" if events else "not_found", "events": events}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_queue_history(args.logistics_no, events)
        return 0 if events else 1
    if not args.execute:
        payload = {"status": "confirmation_required", "message": "该操作会修改本地队列，请增加 --execute。"}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload["message"])
        return 2
    if action == "retry":
        changed = (
            store.retry_email_for_logistics_no(args.logistics_no, reason=args.reason)
            if args.stage == "email"
            else store.retry_stage(args.logistics_no, args.stage, reason=args.reason)
        )
    elif action == "resolve-conflict":
        changed = store.resolve_conflict(args.logistics_no, args.system_order_no, args.platform_order_no)
    elif action == "cancel":
        changed = store.cancel(args.logistics_no, args.reason)
    else:
        changed = False
    payload = {"status": "completed" if changed else "not_changed", "logistics_no": args.logistics_no}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"队列操作结果：{payload['status']}；物流单号：{args.logistics_no}")
    return 0 if changed else 1


def print_queue_list(rows: list[dict]) -> None:
    print("\n自动标发队列")
    print("系统单号 | 平台单号 | 物流单号 | 身份状态 | 物流状态 | ERP状态 | ERP检查点 | 邮件状态 | 原因")
    if not rows:
        print("- | - | - | - | - | - | - | - | -")
        return
    for item in rows:
        print(
            f"{item.get('system_order_no') or '-'} | {item.get('platform_order_no') or '-'} | "
            f"{item.get('logistics_no') or '-'} | {item.get('identity_state') or '-'} | "
            f"{item.get('logistics_state') or '-'} | {item.get('erp_state') or '-'} | "
            f"{item.get('erp_checkpoint') or '-'} | {item.get('email_state') or '-'} | "
            f"{item.get('last_error') or '-'}"
        )


def print_queue_history(logistics_no: str, events: list[dict]) -> None:
    print(f"\n队列事件历史：{logistics_no}")
    print("时间 | 阶段 | 事件 | 原状态 | 新状态 | 说明")
    if not events:
        print("- | - | - | - | - | 未找到记录")
        return
    for event in events:
        print(
            f"{event.get('created_at') or '-'} | {event.get('stage') or '-'} | "
            f"{event.get('event_type') or '-'} | {event.get('old_state') or '-'} | "
            f"{event.get('new_state') or '-'} | {event.get('message') or '-'}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        payload = asyncio.run(run_shipment_scan(args))
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_shipment_scan_result(payload, verbose=bool(getattr(args, "verbose", False)))
        if payload.get("status") == "completed":
            return 0
        if payload.get("status") == "incomplete":
            return SCAN_INCOMPLETE_EXIT_CODE
        return 1
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
        return 0 if payload.get("status") in {"completed", "completed_with_skips"} else 1
    if args.command == "queue":
        return run_queue_cli(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
